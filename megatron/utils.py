# Copyright (c) 2021 Josh Levy-Kramer <josh@levykramer.co.uk>.
# This file is based on code by the authors denoted below and has been modified from its original version.
#
# Copyright (c) 2020, NVIDIA CORPORATION.  All rights reserved.
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

"""General utilities."""
import os
import sys
import re
import time
import socket
from typing import Dict, List

import requests
import wandb
from wandb import UsageError

import torch

from deepspeed.launcher.runner import fetch_hostfile, parse_inclusion_exclusion

from megatron import print_rank_0
from megatron import mpu
from deepspeed import PipelineEngine, DeepSpeedEngine
from collections import deque


def reduce_losses(losses):
    """Reduce a tensor of losses across all GPUs."""
    reduced_losses = torch.cat([loss.clone().detach().view(1) for loss in losses])
    torch.distributed.all_reduce(reduced_losses)
    reduced_losses = reduced_losses / torch.distributed.get_world_size()
    return reduced_losses


def report_memory(name):
    """Simple GPU memory report."""
    mega_bytes = 1024.0 * 1024.0
    string = name + " memory (MB)"
    string += " | allocated: {}".format(torch.cuda.memory_allocated() / mega_bytes)
    string += " | max allocated: {}".format(
        torch.cuda.max_memory_allocated() / mega_bytes
    )
    string += " | reserved: {}".format(torch.cuda.memory_reserved() / mega_bytes)
    string += " | max reserved: {}".format(
        torch.cuda.max_memory_reserved() / mega_bytes
    )
    print_rank_0(string)


def get_attn_mask(seq_length, device):
    """
    Get triangular attention mask for a given sequence length / device.
    """
    # lower triangular attention mask
    mask = torch.tril(torch.ones((1, seq_length, seq_length), device=device)).view(
        1, 1, seq_length, seq_length
    )

    # convert to binary
    return mask < 0.5


def get_ltor_masks_and_position_ids(
    data,
    eod_token,
    eoc_token=None,
    eoc_mask_loss=False,
    eod_mask_loss=False,
):
    """Build masks and position id for left to right model."""

    # Extract batch size and sequence length.
    batch_size, seq_length = data.size()

    # Attention mask (lower triangular).
    attention_mask = get_attn_mask(
        seq_length=seq_length,
        device=data.device,
    )

    # Loss mask.
    loss_mask = torch.ones(data.size(), dtype=torch.float, device=data.device)
    if eod_mask_loss:
        loss_mask[data == eod_token] = 0.0
    
    if eoc_mask_loss:
        if eoc_token is None:
            raise ValueError("`eoc_token` parameter must be supplied to perform conditional finetuning`")

    # Position ids.
    position_ids = torch.arange(seq_length, dtype=torch.long, device=data.device)
    position_ids = position_ids.unsqueeze(0).expand_as(data)

    return attention_mask, loss_mask, position_ids


def local_rank():
    """Local rank of process"""
    local_rank = os.environ.get("LOCAL_RANK")
    if local_rank is None:
        print(
            "utils.local_rank() environment variable LOCAL_RANK not set, defaulting to 0",
            flush=True,
        )
        local_rank = 0
    return int(local_rank)


def is_bnb_available():
    """True if bitsandbytes optimizers are available"""
    return importlib.util.find_spec("bitsandbytes") is not None


def is_local_main():
    """True if is the local main process"""
    return local_rank() == 0


def is_mp_rank_0():
    """True if mp rank == 0"""
    return mpu.get_model_parallel_rank() == 0


def get_wandb_api_key(neox_args):
    """Get Weights and Biases API key from ENV or .netrc file. Otherwise return None"""
    if "WANDB_LOCAL" in os.environ:
        return "LOCAL"
    if "WANDB_API_KEY" in os.environ:
        return os.environ["WANDB_API_KEY"]

    wandb_token = requests.utils.get_netrc_auth(neox_args.wandb_host)

    if wandb_token is not None:
        return wandb_token[1]


def init_wandb(neox_args):
    # Wandb. (one worker per machine)
    if neox_args.use_wandb == False:
        return

    use_wandb = is_local_main() and (get_wandb_api_key(neox_args=neox_args) is not None)
    neox_args.update_value("use_wandb", use_wandb)
    if neox_args.use_wandb:
        group_name = neox_args.wandb_group
        name = f"{socket.gethostname()}-{local_rank()}" if group_name else None
        try:
            wandb.init(
                project=neox_args.wandb_project,
                group=group_name,
                name=name,
                save_code=False,
                force=False,
                entity=neox_args.wandb_team,
            )
        except UsageError as e:
            neox_args.update_value("use_wandb", False)
            print(e)
            print(
                "Skipping wandb. Execute `wandb login` on local or main node machine to enable.",
                flush=True,
            )
        wandb.config.update(neox_args.all_config)


def obtain_resource_pool(
    hostfile_path, include_arg, exclude_arg
) -> Dict[str, List[int]]:
    """
    Get dict of `resource_pool[hostname] = [list of GPU ranks]` using hostfile, include and exclude args.
    Modified from: `deepspeed.launcher.runner.main`
    """
    resource_pool = fetch_hostfile(hostfile_path)
    if not resource_pool:
        resource_pool = {}
        device_count = torch.cuda.device_count()
        if device_count == 0:
            raise RuntimeError("Unable to proceed, no GPU resources available")
        resource_pool["localhost"] = device_count

    active_resources = parse_inclusion_exclusion(
        resource_pool, include_arg, exclude_arg
    )
    return active_resources


def natural_sort(l):
    convert = lambda text: int(text) if text.isdigit() else text.lower()
    alphanum_key = lambda key: [convert(c) for c in re.split("([0-9]+)", key)]
    return sorted(l, key=alphanum_key)


def ddb(rank=0):
    """
    Distributed Debugger that will insert a py debugger on rank `rank` and
    pause all other distributed processes until debugging is complete.
    :param rank:
    """
    if torch.distributed.get_rank() == rank:
        from pdb import Pdb

        pdb = Pdb(skip=["torch.distributed.*"])
        pdb.set_trace(sys._getframe().f_back)
    torch.distributed.barrier()


class Timer:
    """Timer."""

    def __init__(self, name):
        self.name_ = name
        self.elapsed_ = 0.0
        self.started_ = False
        self.start_time = time.time()

    def start(self):
        """Start the timer."""
        assert not self.started_, "timer has already been started"
        torch.cuda.synchronize()
        self.start_time = time.time()
        self.started_ = True

    def stop(self):
        """Stop the timer."""
        assert self.started_, "timer is not started"
        torch.cuda.synchronize()
        self.elapsed_ += time.time() - self.start_time
        self.started_ = False

    def reset(self):
        """Reset timer."""
        self.elapsed_ = 0.0
        self.started_ = False

    def elapsed(self, reset=True):
        """Calculate the elapsed time."""
        started_ = self.started_
        # If the timing in progress, end it first.
        if self.started_:
            self.stop()
        # Get the elapsed time.
        elapsed_ = self.elapsed_
        # Reset the elapsed time
        if reset:
            self.reset()
        # If timing was in progress, set it back.
        if started_:
            self.start()
        return elapsed_


class Timers:
    """Group of timers."""

    def __init__(self, use_wandb, tensorboard_writer):
        self.timers = {}
        self.use_wandb = use_wandb
        self.tensorboard_writer = tensorboard_writer

    def __call__(self, name):
        if name not in self.timers:
            self.timers[name] = Timer(name)
        return self.timers[name]

    def write(self, names, iteration, normalizer=1.0, reset=False):
        """Write timers to a tensorboard writer"""
        # currently when using add_scalars,
        # torch.utils.add_scalars makes each timer its own run, which
        # polutes the runs list, so we just add each as a scalar
        assert normalizer > 0.0
        for name in names:
            value = self.timers[name].elapsed(reset=reset) / normalizer

            if self.tensorboard_writer:
                self.tensorboard_writer.add_scalar(f"timers/{name}", value, iteration)

            if self.use_wandb:
                wandb.log({f"timers/{name}": value}, step=iteration)

    def log(self, names, normalizer=1.0, reset=True):
        """Log a group of timers."""
        assert normalizer > 0.0
        string = "time (ms)"
        for name in names:
            elapsed_time = self.timers[name].elapsed(reset=reset) * 1000.0 / normalizer
            string += " | {}: {:.2f}".format(name, elapsed_time)
        if torch.distributed.is_initialized():
            if torch.distributed.get_rank() == 0:
                print(string, flush=True)
        else:
            print(string, flush=True)


def expand_attention_types(attention_config, num_layers):
    """
    Expands an `attention_config` list in the following format:

        [
        [['attention_type_1', ..., `attention_type_n`], 12]
        ]

    to a flattened list of length `num_layers`.

    :param params_list:
    :return:
    """
    # if only strings are found in the config, we assume it's already expanded
    if all([isinstance(i, str) for i in attention_config]):
        return attention_config
    newlist = []
    for item in attention_config:
        # instead of specifying a number - we can specify 'all' to extend this pattern across all layers
        if item[1] == "all":
            assert num_layers % len(item[0]) == 0, (
                f"Number of layers ({num_layers}) is not divisible by the length "
                f"of pattern: {item[0]}"
            )
            return item[0] * (num_layers // len(item[0]))
        for _ in range(item[1]):
            newlist.extend(item[0])
    return newlist


class OverflowMonitor:

    """
    Checks if the past n iterations have been skipped due to overflow, and exits
    training if that happens.
    """

    def __init__(self, optimizer, n=50):
        self.optimizer = optimizer
        self.n = n
        self.history = deque(maxlen=n)

    def check(self, skipped):
        self.history.append(skipped)
        if (
            self.optimizer.overflow
            and len(self.history) == self.n
            and all(self.history)
        ):
            raise Exception(
                f"Skipped {self.n} iterations in a row due to Overflow - Exiting training."
            )


def get_noise_scale_logger(neox_args):
    if neox_args.log_gradient_noise_scale:
        if neox_args.zero_stage >= 1:
            raise NotImplementedError(
                "Gradient Noise Scale logging does not work with zero stage 2+, as the "
                "gradients are distributed across ranks."
            )
        noise_scale_logger = GradientNoiseScale(
            model=model,
            batch_size_small=neox_args.train_batch_size,
            n_batches=neox_args.gradient_noise_scale_n_batches,
            cpu_offload=neox_args.gradient_noise_scale_cpu_offload,
            neox_args=neox_args,
            mpu=mpu,
        )
    else:
        noise_scale_logger = None
    return noise_scale_logger


def get_total_params(model):
    # Print number of parameters.
    if mpu.get_data_parallel_rank() == 0:
        params = sum([p.nelement() for p in model.parameters()])
        print(
            " > number of parameters on model parallel rank {}: {}".format(
                mpu.get_model_parallel_rank(), params
            ),
            flush=True,
        )
    else:
        params = 0

    total_n_parameters = torch.tensor([params]).cuda(torch.cuda.current_device())
    torch.distributed.all_reduce(total_n_parameters)
    total_n_parameters = total_n_parameters.item()
    return total_n_parameters


def setup_for_inference_or_eval(
    use_cache=True,
    overwrite_values=None,
):
    """
    Initializes the model for evaluation or inference (doesn't load optimizer states, etc.) from command line args.

    use_cache: bool
        Whether to use key value caching in inference.
    overwrite_values: dict
        Optional Values to overwrite in the model config.
    """

    from megatron.neox_arguments import NeoXArgs
    from megatron.initialize import initialize_megatron
    from megatron.training import setup_model_and_optimizer

    _overwrite_values = {
        "checkpoint_activations": False,
        "partition_activations": False,
        "no_load_optim": True,
        "zero_optimization": None,  # disable zero optimization (won't be used in inference, and loading zero optimizer can cause errors)
    }
    if overwrite_values:
        _overwrite_values.update(overwrite_values)
    neox_args = NeoXArgs.consume_neox_args(overwrite_values=_overwrite_values)
    neox_args.configure_distributed_args()
    neox_args.build_tokenizer()

    if neox_args.load is None:
        raise ValueError("`load` parameter must be supplied to load a model`")

    # initialize megatron
    initialize_megatron(neox_args)

    # set up model and load checkpoint.
    model, _, _ = setup_model_and_optimizer(
        neox_args=neox_args,
        use_cache=use_cache,
        iteration=neox_args.iteration,
    )  # we use setup_model_and_optimizer instead of get_model in order to initialize deepspeed
    print_rank_0("Finished loading model")

    model.module.inference_mode(use_cache=use_cache)
    return model, neox_args


class CharCounter:
    """
    Wraps the data_iterator to count the number of characters in a batch
    """

    def __init__(self, data_iterator, tokenizer):
        self.tokenizer = tokenizer
        self.data_iterator = data_iterator
        self.char_count = 0
        self.batch_count = 0
        self.token_count = 0
        self.total_time = 0

    def tokens_per_char(self):
        return self.token_count / self.char_count

    def __iter__(self):
        return self

    def __next__(self):
        start = time.time()
        batch = self.data_iterator.__next__()
        for b in batch["text"]:
            self.token_count += len(b)
            self.char_count += len(self.tokenizer.detokenize(b.tolist()))
        self.batch_count += 1
        end = time.time()
        self.total_time += end - start
        return batch
