import collections
import random
from typing import Any, Dict, Optional, Union

import mesh_tensorflow as mtf
import tensorflow as tf
import tensorflow.compat.v1 as v1
from absl import app, logging
from absl.flags import argparse_flags
from pydantic import BaseModel, validator
from pydantic.dataclasses import dataclass

import config
import datasets
import models
import inputs 
import devices

from devices.tpu import TPUJobSpec

@dataclass
class ScheduleSpec:
    steps: int
    steps_per_checkpoint: int
    steps_per_iteration: int

@dataclass
class RunSpec:
    learning_rate: Dict
    optimizer: Dict

@dataclass
class TrainerConfig:
    device: Dict
    infeed: Dict
    model: Dict
    other: Any
    regularization: Dict
    runspec: RunSpec
    schedule: ScheduleSpec
    #checkpoints_location: str
    model_path: str

    @classmethod
    def from_config(cls, location):
        config_dict = config.load(location)
        return cls(**config_dict)


class Trainer:
    def __init__(self, config: TrainerConfig):
        self.config = config
        # self.device = "tpu" if config.tpu else "cpu"
        self.infeed = None
        self.model = None
        self.device = None

    def save_checkpoint(self):
        state = self.model.state_dict()
        logging.info("saving model checkpoint to %s", self.config.ckpt_path)
        self.save(state, self.config.ckpt_path)
        logging.info("saved model checkpoint to %s", self.config.ckpt_path)

    def load_model(self):
        if not (self.model is None): return self.model
        self.model = models.from_config(self.config.model)
        return self.model
            
    def load_infeed(self):
        if not (self.infeed is None): return self.infeed
        self.infeed = inputs.from_config(self.config.infeed)
        return self.infeed

    def create_jobspec(self):
        model=self.load_model()
        infeed=self.load_infeed()
        return TPUJobSpec(
            function=self.model,
            params={},
            max_steps=1000,
            model_path=self.config.model_path,
            steps_per_iteration=self.config.schedule.steps_per_iteration,
            steps_per_checkpoint=self.config.schedule.steps_per_checkpoint,
            batch_size=infeed.config.batch_size,
        )

    def execute(self, jobspec):
        if self.device is None:
            self.device = devices.from_config(self.config.device) 
        self.device.execute(jobspec)

    def train(self):
        model, config = self.model, self.config
        # raw_model = model.module if hasattr(self.model, "module") else model
        # optimizer = raw_model.configure_optimizers(config)

        # def run_epoch(split):
        # is_train = split == 'train'
        # model.train(is_train)
        # data = self.dataset
        # loader = DataLoader(data, shuffle=True, pin_memory=True,
        #                     batch_size=config.batch_size,
        #                     num_workers=config.num_workers)

        # losses = []
        # pbar = tqdm(enumerate(loader), total=len(loader)) if is_train else enumerate(loader)
        # for it, (x, y) in pbar:

        #     # place data on the correct device
        #     x = x.to(self.device)
        #     y = y.to(self.device)

        #     # forward the model
        #     with torch.set_grad_enabled(is_train):
        #         logits, loss = model(x, y)
        #         loss = loss.mean() # collapse all losses if they are scattered on multiple gpus
        #         losses.append(loss.item())

        #     if is_train:

        #         # backprop and update the parameters
        #         model.zero_grad()
        #         loss.backward()
        #         torch.nn.utils.clip_grad_norm_(model.parameters(), config.grad_norm_clip)
        #         optimizer.step()

        #         # decay the learning rate based on our progress
        #         if config.lr_decay:
        #             self.tokens += (y >= 0).sum() # number of tokens processed this step (i.e. label is not -100)
        #             if self.tokens < config.warmup_tokens:
        #                 # linear warmup
        #                 lr_mult = float(self.tokens) / float(max(1, config.warmup_tokens))
        #             else:
        #                 # cosine learning rate decay
        #                 progress = float(self.tokens - config.warmup_tokens) / float(max(1, config.final_tokens - config.warmup_tokens))
        #                 lr_mult = max(0.1, 0.5 * (1.0 + math.cos(math.pi * progress)))
        #             lr = config.learning_rate * lr_mult
        #             for param_group in optimizer.param_groups:
        #                 param_group['lr'] = lr
        #         else:
        #             lr = config.learning_rate

        #         # report progress
        #         pbar.set_description(f"epoch {epoch+1} iter {it}: train loss {loss.item():.5f}. lr {lr:e}")

        # if not is_train:
        #     test_loss = float(np.mean(losses))
        #     logger.info("test loss: %f", test_loss)
        #     return test_loss

        # best_loss = float('inf')
        # self.tokens = 0 # counter used for learning rate decay
        # for epoch in range(config.max_epochs):

        #     run_epoch('train')
        #     if self.test_dataset is not None:
        #         test_loss = run_epoch('test')

        #     # supports early stopping based on the test loss, or just save always if no test set is provided
        #     good_model = self.test_dataset is None or test_loss < best_loss
        #     if self.config.ckpt_path is not None and good_model:
        #         best_loss = test_loss
        #         self.save_checkpoint()


# def load_trainer(args) -> Trainer:
#     #     config.load
#     # #     with tf.io.gfile.GFile(args.config) as fd:
#     # #         params = json.load(fd)
#     # #     cfg = TrainerConfig(**params)
#     # #     json.dump(params, sys.stdout, indent=2)
#     config_dict = config.load(args.runspec)
#     trainer = TrainerConfig(**config_dict)
#     return trainer

    # if args.testrun:
    #    pass
    # rewire to use testing related functions if --test is on
    # return Trainer(
    #     name='test',
    #     config=cfg,
    #     model_fn=lambda *args: None,
    #     input_fn=load_input_fn(cfg.infeed),
    #     # pred_input_fn=test_pred_input,
    #     handle_prediction_output_fn=test_handle_pred_output
    # )


#     if args.model == '':
#         raise ValueError('Model must be set')

#     # params = load_trainer_config(args.model)

#     # Fetch encoder per params
#     encoder = fetch_encoder(params)

#     # model.pred_input_fn = partial(pred_input_fn, enc = encoder)

#     return Trainer(
#         name=args.model,
#         input_fn=generic_text,
#         config=cfg,
#         # pred_input_fn=pred_input,
#         handle_prediction_output_fn=handle_pred_output,
#     )

def check_dataset(trainer, args):
    steps = trainer.config.schedule.steps
    infeed = trainer.load_infeed()

    logging.info('running for %d steps', steps)
    with v1.Session(graph=tf.Graph()) as sess:
        ds = infeed()

        it = ds.make_one_shot_iterator()
        example = it.get_next()
        for i in range(steps):
            try:
                result = sess.run(example)
                logging.info('%d/%d: %r', i, steps, result)
            except tf.errors.OutOfRangeError:
                logging.error('dataset ended prematurely after only %d of the %d expected steps', i, steps)

def parse_args(args, parser=None):
    # Parse command line arguments
    parser.add_argument(
        "runspec",
        type=str,
        help="the json file specifiing the configuration for this run",
    )  # Name of TPU to train on, if any
    parser.add_argument("--testrun", action="store_true", default=False)
    parser.add_argument("--check-dataset", action="store_true", default=False)
    parser.add_argument('--tpu', type=str,  help='Name of TPU to train on, (if any)')

def local_parse_args(args):
    parser = argparse_flags.ArgumentParser()
    parse_args(args, parser)
    return parser.parse_args(args[1:])


def main(args):
    logging.info("started train process")

    tconfig = TrainerConfig.from_config(args.runspec)

    # patch config 
    if args.tpu:
        tconfig.device.address = args.tpu

    trainer = Trainer(tconfig)

    if args.check_dataset:
        check_dataset(trainer, args)
         
    # saves config to logdir for experiment management
    # save_config(pprint.pformat(params), params["model_path"])
    # save_config(params, params["model_path"])

    trainer.load_model()

    j = trainer.create_jobspec()
    j.train = True
    trainer.execute(j)
    
    #estimator.train(input_fn=partial(input_fn, eval=False), max_steps=params["train_steps"])

    # train
    logging.info("completed train process")


if __name__ == "__main__":
    tf.disable_v2_behavior()
    app.run(main, flags_parser=local_parse_args)
