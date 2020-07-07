# coding=utf-8
# Copyright 2020 The Mesh TensorFlow Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""MNIST using Mesh TensorFlow and TF Estimator.

This is an illustration, not a good model.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import mesh_tensorflow as mtf
import mnist_dataset as dataset  # local file import
import tensorflow as tf
import os

tf.flags.DEFINE_string("data_dir", "/tmp/mnist_data",
                       "Path to directory containing the MNIST dataset")
tf.flags.DEFINE_string("model_dir", "/tmp/mnist_model", "Estimator model_dir")
tf.flags.DEFINE_integer("batch_size", 2000,
                        "Mini-batch size for the training. Note that this "
                        "is the global batch size and not the per-shard batch.")
tf.flags.DEFINE_integer("hidden_size", 512, "Size of each hidden layer.")
tf.flags.DEFINE_integer("train_epochs", 40, "Total number of training epochs.")
tf.flags.DEFINE_integer("epochs_between_evals", 1,
                        "# of epochs between evaluations.")
tf.flags.DEFINE_integer("eval_steps", 0,
                        "Total number of evaluation steps. If `0`, evaluation "
                        "after training is skipped.")
tf.flags.DEFINE_string("mesh_shape", "b1:2;b2:2", "mesh shape")
tf.flags.DEFINE_string("layout", "row_blocks:b1;col_blocks:b2",
                       "layout rules")

FLAGS = tf.flags.FLAGS


def mnist_model(image, labels, mesh):
    """The model.

    Args:
      image: tf.Tensor with shape [batch, 28*28]
      labels: a tf.Tensor with shape [batch] and dtype tf.int32
      mesh: a mtf.Mesh

    Returns:
      logits: a mtf.Tensor with shape [batch, 10]
      loss: a mtf.Tensor with shape []
    """

    batch_dim = mtf.Dimension("batch", FLAGS.batch_size)  # define named dimensions
    row_blocks_dim = mtf.Dimension("row_blocks", 4)
    col_blocks_dim = mtf.Dimension("col_blocks", 4)
    rows_dim = mtf.Dimension("rows_size", 7)
    cols_dim = mtf.Dimension("cols_size", 7)
    classes_dim = mtf.Dimension("classes", 10)
    one_channel_dim = mtf.Dimension("one_channel", 1)

    x = mtf.import_tf_tensor(
        mesh, tf.reshape(image, [FLAGS.batch_size, 4, 7, 4, 7, 1]),
        mtf.Shape(
            [batch_dim, row_blocks_dim, rows_dim,
             col_blocks_dim, cols_dim, one_channel_dim]))
    x = mtf.transpose(x, [
        batch_dim, row_blocks_dim, col_blocks_dim,
        rows_dim, cols_dim, one_channel_dim])

    # add some convolutional layers to demonstrate that convolution works.
    filters1_dim = mtf.Dimension("filters1", 16)
    filters2_dim = mtf.Dimension("filters2", 16)
    f1 = mtf.relu(mtf.layers.conv2d_with_blocks(
        x, filters1_dim, filter_size=[9, 9], strides=[1, 1], padding="SAME",
        h_blocks_dim=row_blocks_dim, w_blocks_dim=col_blocks_dim, name="conv0"))
    f2 = mtf.relu(mtf.layers.conv2d_with_blocks(
        f1, filters2_dim, filter_size=[9, 9], strides=[1, 1], padding="SAME",
        h_blocks_dim=row_blocks_dim, w_blocks_dim=col_blocks_dim, name="conv1"))
    x = mtf.reduce_mean(f2, reduced_dim=filters2_dim)

    # add some fully-connected dense layers.
    hidden_dim1 = mtf.Dimension("hidden1", FLAGS.hidden_size)
    hidden_dim2 = mtf.Dimension("hidden2", FLAGS.hidden_size)

    h1 = mtf.layers.dense(
        x, hidden_dim1,
        reduced_dims=x.shape.dims[-4:],
        activation=mtf.relu, name="hidden1")
    h2 = mtf.layers.dense(
        h1, hidden_dim2,
        activation=mtf.relu, name="hidden2")
    logits = mtf.layers.dense(h2, classes_dim, name="logits")
    if labels is None:
        loss = None
    else:
        labels = mtf.import_tf_tensor(
            mesh, tf.reshape(labels, [FLAGS.batch_size]), mtf.Shape([batch_dim]))
        loss = mtf.layers.softmax_cross_entropy_with_logits(
            logits, mtf.one_hot(labels, classes_dim), classes_dim)
        loss = mtf.reduce_mean(loss)
    return logits, loss


def model_fn(features, labels, mode, params):
    """The model_fn argument for creating an Estimator."""
    tf.logging.info("features = %s labels = %s mode = %s params=%s" %
                    (features, labels, mode, params))
    global_step = tf.train.get_global_step()

    # define mtf graph, mesh size & layout rules
    graph = mtf.Graph()
    mesh_shape = mtf.convert_to_shape(FLAGS.mesh_shape)
    mesh_size = mesh_shape.size
    mesh_devices = [''] * mesh_size

    layout_rules = mtf.convert_to_layout_rules(FLAGS.layout)

    ctx = params['context']
    num_hosts = ctx.num_hosts
    host_placement_fn = ctx.tpu_host_placement_function
    device_list = [host_placement_fn(host_id=t) for t in range(num_hosts)]
    tf.logging.info('device_list = %s' % device_list, )
    # TODO: Better estimation of replica cache size?
    replica_cache_size = 300 * 1000000  # 300M per replica
    # Worker 0 caches all the TPU binaries.
    worker0_mem = replica_cache_size * ctx.num_replicas
    devices_memory_usage = [worker0_mem] + [0] * (num_hosts - 1)
    var_placer = mtf.utils.BalancedVariablePlacer(device_list,
                                                  devices_memory_usage)
    mesh_impl = mtf.simd_mesh_impl.SimdMeshImpl(
        mesh_shape, layout_rules, mesh_devices, ctx.device_assignment)
    mesh = mtf.Mesh(graph, "my_mesh", var_placer)

    # run model
    logits, loss = mnist_model(features, labels, mesh)

    if mode == tf.estimator.ModeKeys.TRAIN:
        var_grads = mtf.gradients(
            [loss], [v.outputs[0] for v in graph.trainable_variables])
        optimizer = mtf.optimize.AdafactorOptimizer()
        update_ops = optimizer.apply_grads(var_grads, graph.trainable_variables)

    lowering = mtf.Lowering(graph, {mesh: mesh_impl})
    tf_logits = lowering.export_to_tf_tensor(logits)
    if mode != tf.estimator.ModeKeys.PREDICT:
        tf_loss = lowering.export_to_tf_tensor(loss)

        # tf.summary.scalar("loss", tf_loss)
    with mtf.utils.outside_all_rewrites():

        restore_hook = mtf.MtfRestoreHook(lowering)

        if mode == tf.estimator.ModeKeys.TRAIN:
            tf_update_ops = [lowering.lowered_operation(op) for op in update_ops]
            tf_update_ops.append(tf.assign_add(global_step, 1))
            train_op = tf.group(tf_update_ops)
            saver = tf.train.Saver(
                tf.global_variables(),
                sharded=True,
                max_to_keep=10,
                keep_checkpoint_every_n_hours=2,
                defer_build=False, save_relative_paths=True)
            tf.add_to_collection(tf.GraphKeys.SAVERS, saver)
            saver_listener = mtf.MtfCheckpointSaverListener(lowering)
            saver_hook = tf.train.CheckpointSaverHook(
                FLAGS.model_dir,
                save_steps=1000,
                saver=saver,
                listeners=[saver_listener])

            # Name tensors to be logged with LoggingTensorHook.
            # tf.identity(tf_loss, "cross_entropy")
            # tf.identity(accuracy[1], name="train_accuracy")

            # Save accuracy scalar to Tensorboard output.
            # tf.summary.scalar("train_accuracy", accuracy[1])

            # restore_hook must come before saver_hook

            return tf.compat.v1.estimator.tpu.TPUEstimatorSpec(
                tf.estimator.ModeKeys.TRAIN, loss=tf_loss, train_op=train_op,
                training_hooks=[restore_hook, saver_hook])

    if mode == tf.estimator.ModeKeys.PREDICT:
        predictions = {
            "classes": tf.argmax(tf_logits, axis=1),
            "probabilities": tf.nn.softmax(tf_logits),
        }
        return tf.estimator.EstimatorSpec(
            mode=tf.estimator.ModeKeys.PREDICT,
            predictions=predictions,
            prediction_hooks=[restore_hook],
            export_outputs={
                "classify": tf.estimator.export.PredictOutput(predictions)
            })
    if mode == tf.estimator.ModeKeys.EVAL:
        return tf.estimator.EstimatorSpec(
            mode=tf.estimator.ModeKeys.EVAL,
            loss=tf_loss,
            evaluation_hooks=[restore_hook],
            eval_metric_ops={
                "accuracy":
                    tf.metrics.accuracy(
                        labels=labels, predictions=tf.argmax(tf_logits, axis=1)),
            })


def get_tpu_resolver(tpu_name='auto'):
    # Get the TPU's location
    if tpu_name != 'auto':
        return tf.distribute.cluster_resolver.TPUClusterResolver(tpu_name)
    elif 'COLAB_TPU_ADDR' in os.environ:
        return tf.distribute.cluster_resolver.TPUClusterResolver()
    elif 'TPU_NAME' in os.environ:
        return tf.distribute.cluster_resolver.TPUClusterResolver(os.environ['TPU_NAME'])


def run_mnist():
    """Run MNIST training and eval loop."""

    global_step = tf.train.get_global_step()
    mesh_shape = mtf.convert_to_shape(FLAGS.mesh_shape)

    tpu_config = tf.contrib.tpu.TPUConfig(num_shards=mesh_shape.size,
                                          iterations_per_loop=128,
                                          experimental_host_call_every_n_steps=64,
                                          num_cores_per_replica=1,
                                          per_host_input_for_training=tf.compat.v1.estimator.tpu.InputPipelineConfig.BROADCAST)

    # mnist_classifier = tf.estimator.Estimator(
    #   model_fn=model_fn,
    #   model_dir=FLAGS.model_dir)

    run_config = tf.contrib.tpu.RunConfig(
        model_dir=FLAGS.model_dir,
        # save_checkpoints_steps=100,
        save_checkpoints_secs=None,
        keep_checkpoint_max=None,
        cluster=get_tpu_resolver(),
        tpu_config=tpu_config)

    mnist_classifier = tf.contrib.tpu.TPUEstimator(
        config=run_config,
        use_tpu=True,
        model_fn=model_fn,
        train_batch_size=FLAGS.batch_size,
        eval_batch_size=FLAGS.batch_size)

    print('Training...')

    # mnist_classifier.train(input_fn, steps=training_steps)

    # Set up training and evaluation input functions.
    def train_input_fn(params):
        """Prepare data for training."""
        batch_size = params["batch_size"]
        # When choosing shuffle buffer sizes, larger sizes result in better
        # randomness, while smaller sizes use less memory. MNIST is a small
        # enough dataset that we can easily shuffle the full epoch.
        ds = dataset.train(FLAGS.data_dir)
        ds_batched = ds.cache().shuffle(buffer_size=50000).repeat(10000).batch(batch_size, drop_remainder=True)

        # Iterate through the dataset a set number (`epochs_between_evals`) of times
        # during each training session.
        ds = ds_batched
        return ds

    def eval_input_fn():
        return dataset.test(FLAGS.data_dir).batch(
            FLAGS.batch_size).make_one_shot_iterator().get_next()

    # Train and evaluate model.
    for _ in range(FLAGS.train_epochs // FLAGS.epochs_between_evals):
        mnist_classifier.train(input_fn=train_input_fn, max_steps=10000)
        # eval_results = mnist_classifier.evaluate(input_fn=eval_input_fn)
        # print("\nEvaluation results:\n\t%s\n" % eval_results)


def main(_):
    run_mnist()


if __name__ == "__main__":
    # tf.disable_v2_behavior()
    tf.logging.set_verbosity(tf.logging.INFO)
    tf.app.run()
