import mesh_tensorflow as mtf
import tensorflow.compat.v1 as tf
import tensorflow.compat.v2 as tf2
from tensorflow.python.tpu import tpu_estimator

from .dataclass import ModelParameter
from .optimizers import get_optimizer

tf.config.optimizer.set_experimental_options({"layout_optimizer": True,
                                              "constant_folding": True,
                                              "shape_optimization": True,
                                              "remapping": True,
                                              "arithmetic_optimization": True,
                                              "dependency_optimization": True,
                                              "loop_optimization": True,
                                              "function_optimization": True,
                                              "debug_stripper": True,
                                              "disable_model_pruning": False,
                                              "scoped_allocator_optimization": True,
                                              "pin_to_host_optimization": False,
                                              "implementation_selector": True,
                                              "auto_mixed_precision": True,
                                              "disable_meta_optimizer": False,
                                              "min_graph_nodes": 0
                                              })


def create_host_call(model_dir):
    """Construct a host_call writing scalar summaries.
    Borrowed from t2t.

    Args:
        model_dir: String containing path to train
    Returns:
        (fn, args) Pair to be called by TPUEstimator as the host_call.
    """

    graph = tf.get_default_graph()
    # A list of (name, lowered tensor) tuples
    summaries = graph.get_collection(mtf.utils.SCALAR_SUMMARIES_COLLECTION_KEY)

    def maybe_cast(tensor):
        if tensor.shape.is_compatible_with([]):
            tensor = tf.reshape(tensor, [1])
        if tensor.dtype == tf.int64:
            return tf.cast(tensor, tf.int32)
        if tensor.dtype == tf.bfloat16:
            return tf.cast(tensor, tf.float32)
        return tensor

    reshaped_tensors = [maybe_cast(t) for _, t in summaries]

    # When no supported summaries are found, don't create host_call. Otherwise,
    # TPU outfeed queue would enqueue global_step while host_call doesn't dequeue
    # it, eventually causing hang.
    if not reshaped_tensors:
        return None

    def host_call_fn(global_step, *args):
        """Training host call. Creates scalar summaries for training metrics."""
        # This function is executed on the CPU and should not directly reference
        # any Tensors in the rest of the `model_fn`. To pass Tensors from the
        # model to the `model_fn`, provide as part of the `host_call`.
        global_step = tf.cast(global_step[0], tf.int64)
        with tf2.summary.create_file_writer(model_dir).as_default():
            # We cannot directly use any tensor from summaries, because each
            # tensor here must be a concat of multiple tensors from all shards.
            # Therefore, we rely on the assumption that args wil have the same
            # length as summaries, and all tensors in args will have the same
            # order of self._tup_summaries.
            assert len(args) == len(summaries)
            for i, tensor in enumerate(args):
                name = summaries[i][0]
                tf2.summary.scalar(name, tf.reduce_mean(tensor), step=global_step)
        return tf.summary.all_v2_summary_ops()

    global_step_t = tf.reshape(tf.cast(tf.train.get_global_step(), tf.int32), [1])
    return host_call_fn, [global_step_t] + reshaped_tensors


def model_fn(features: tf.Tensor, mode: str, params: dict):
    # Get global step
    params = ModelParameter(params)
    global_step = tf.train.get_global_step()

    # Construct mtf graph + mesh from params
    graph = mtf.Graph()
    mesh_shape = mtf.convert_to_shape(params.mesh_shape)
    layout_rules = mtf.convert_to_layout_rules(params.layout)

    # Mesh setup
    num_hosts = params.context.num_hosts
    host_placement_fn = params.context.tpu_host_placement_function
    device_list = [host_placement_fn(host_id=i) for i in range(num_hosts)]
    tf.logging.info(f"device_list = {device_list}")

    var_placer = mtf.utils.BalancedVariablePlacer(device_list,
                                                  [300 * 1000000 * params.context.num_replicas] + [0] * (num_hosts - 1))
    mesh_devices = [""] * mesh_shape.size
    mesh_impl = mtf.simd_mesh_impl.SimdMeshImpl(
        mesh_shape, layout_rules, mesh_devices, params.context.device_assignment)

    # Trainable variable precision
    # Store to checkpoints in master type, train in slice type, compute in activation type
    variable_dtype = mtf.VariableDType(master_dtype=tf.float32, slice_dtype=tf.float32, activation_dtype=tf.float32)

    # Build mtf mesh object
    mesh = mtf.Mesh(graph, "mesh", var_placer)
    params.mesh = mesh

    # Build mtf_features & seq length dict for getting number of microbatches
    # We need to pack inputs into a dict to pass into serialize_training_step
    params.mode = mode
    batch_dim = mtf.Dimension("batch", params.train_batch_size)

    frame_input = None
    token_x_input = None
    token_y_input = None
    frame_mask = None
    token_mask = None

    if params.model_mode == 'jannet':
        frame_input_shape = [batch_dim, mtf.Dimension("_sequence", params.time_patch_size + 1)]

        if params.three_axes:
            frame_input_shape = frame_input_shape + [mtf.Dimension("height", params.frame_height_patch),
                                                     mtf.Dimension("width", params.frame_width_patch)]
        else:
            frame_input_shape = frame_input_shape + [mtf.Dimension("height", params.frame_height_patch
                                                                   * params.frame_width_patch)]

        frame_input_shape = frame_input_shape + [mtf.Dimension("color_channels", params.channel_color_size)]

        frame_input = mtf.import_fully_replicated(mesh, features['frame'], mtf.Shape(frame_input_shape), "frame_input")

        if params.use_language:
            sequence_dim = mtf.Dimension("sequence", params.time_patch_size)

            token_dim_shape = [batch_dim,
                               sequence_dim,
                               mtf.Dimension("height", params.language_token_patch),
                               mtf.Dimension("language_token_patch", params.token_patch_size)]

            frame_mask_shape = mtf.Shape([batch_dim, sequence_dim])

            token_x_input = mtf.import_fully_replicated(mesh, features['token_x'], token_dim_shape, "tkn_src")
            token_y_input = mtf.import_fully_replicated(mesh, features['token_y'], token_dim_shape, "tkn_tgt")

            frame_mask = mtf.import_fully_replicated(mesh, features['frame_mask'], frame_mask_shape, "frame_mask")
            token_mask = mtf.import_fully_replicated(mesh, features['token_mask'], token_dim_shape, "token_mask")

    elif params.model_mode == 'gpt':

        token_dim_shape = [batch_dim,
                           mtf.Dimension("sequence", params.time_patch_size),
                           mtf.Dimension("language_tokens", params.language_token_per_frame)]

        token_x_input = mtf.import_fully_replicated(mesh, features['token_x'], token_dim_shape, "tkn_src")
        token_y_input = mtf.import_fully_replicated(mesh, features['token_y'], token_dim_shape, "tkn_tgt")

    else:
        raise ValueError("model_mode need to be 'jannet' or 'gpt' {}, "
                         "is a not supported option.".format(params.model_mode))

    with mtf.utils.outside_all_rewrites():
        with tf.variable_scope('jannet'):
            logits, loss, video_loss, token_loss = params.build(frame_input, token_x_input, token_y_input,
                                                                frame_mask, token_mask)

    _, update_ops, var_grads = get_optimizer(mesh, loss, params, var_grads=None)

    if params.use_video:
        mtf.scalar_summary("video_loss", video_loss)

    if params.use_language:
        mtf.scalar_summary("token_loss", token_loss)

    total_parameters = 0
    for variable in graph.trainable_variables:
        shape = variable.shape.dims
        variable_parameters = 1

        for dim in shape:
            variable_parameters *= dim.size
        total_parameters += variable_parameters

    print(f"\n\nN TRAINABLE VARS:\n{total_parameters:,}\n\n")
    all_dim_names = []

    for variable in graph.all_variables:
        names = variable.shape.dimension_names
        all_dim_names.append(names)

    # Print all dim names in graph & write to file
    all_dim_names = [item for sublist in all_dim_names for item in sublist]  # Flatten all dims
    unique_dims = list(set(all_dim_names))
    print("ALL DIM NAMES:")
    for dim_name in unique_dims:
        print(dim_name)
    print('\n')

    lowering = mtf.Lowering(graph, {mesh: mesh_impl}, autostack=True)

    tf_loss = lowering.export_to_tf_tensor(loss)
    tf_loss = tf.cast(tf_loss, tf.float32)

    # Use our patched version until mtf updates theirs
    host_call = create_host_call(params.model_path)
    mtf.utils.remove_summaries()

    # Creates train_op
    tf_update_ops = [lowering.lowered_operation(op) for op in update_ops]
    tf_update_ops.append(tf.assign_add(global_step, 1))  # Need to manually increment global_step
    tf.logging.info(f"tf_update_ops: {tf_update_ops}")
    train_op = tf.group(tf_update_ops)

    with mtf.utils.outside_all_rewrites():
        restore_hook = mtf.MtfRestoreHook(lowering)
        return tpu_estimator.TPUEstimatorSpec(
                tf.estimator.ModeKeys.TRAIN,
                loss=tf_loss,
                host_call=host_call,
                training_hooks=[restore_hook],
                train_op=train_op)
