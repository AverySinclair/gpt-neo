local optimizers = import 'optimizers.libsonnet';

local lr() = {
   lr: 0.0001,
   lr_decay: "cosine",
   warmup_steps: 0,
};

local Dataset() = {
   kind: 'tfrecord',
   sources: ['/tmp/uds-preprocess/*.tfrecord'],
};

local GPT2() = {
   type: "GPT2",
   n_ctx: 8,
   n_embd: 8,
   n_head: 8,
   n_vocab: 32,
   n_layer: 1,
   scale_by_depth: false,
   scale_by_in: false,
   mesh_shape: "batch:1",
   layout: "batch:1",
   activation_function: "gelu",
   attention_types: [
      [["global"], 1]
   ],
   auto_layout: false,
   auto_layout_and_mesh_shape: false,
};

local Other() = {
   stop_at_token: 2,
   remove_partial_sequences: true,
   scalenorm: true,
   no_weight_tie: false,
};

local InFeed() = {
   batch_size: 8,
   random: {
      context_length: 8,
      vocab_size: 16000,
   },
   dataset: Dataset(),
};

local Schedule() = {
   steps: 100,                // total number of steps to run
   steps_per_checkpoint: 100, // save a checkpoint after this num of steps
   steps_per_iteration: 500,  // how many steps to loop on-device
};

local TPU() = {
   num_cores: 8,
};

local CPU() = {
   num_cores: 1,
};

local Trainer() = {
   device: CPU(),
   infeed: InFeed(),
   model: GPT2(),
   model_path: "/tmp/checkpoints/{runid}",
   runspec: {
      optimizer: optimizers.Adam(),
      // model_path: std.extVar('MODEL_PATH'), // the location to save the checkpoints
      learning_rate: lr(),
   },
   schedule: Schedule(),
   regularization: {
      embed_dropout: 0.1,
      weight_decay: 0.1,
      attn_dropout: 0.1,
      res_dropout:0.1,
      gradient_clipping: 0.5,
   },
   other: Other()
};

Trainer() // main configuration