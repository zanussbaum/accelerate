#!/usr/bin/env python

# Copyright 2021 The HuggingFace Team. All rights reserved.
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

import contextlib
import io

import torch
from torch.utils.data import DataLoader

from accelerate import Accelerator
from accelerate.data_loader import prepare_data_loader
from accelerate.state import AcceleratorState
from accelerate.test_utils import RegressionDataset, RegressionModel, are_the_same_tensors
from accelerate.utils import (
    DistributedType,
    gather,
    is_bf16_available,
    is_torch_version,
    set_seed,
    synchronize_rng_states,
)


def print_main(state):
    print(f"Printing from the main process {state.process_index}")


def print_local_main(state):
    print(f"Printing from the local main process {state.local_process_index}")


def print_last(state):
    print(f"Printing from the last process {state.process_index}")


def print_on(state, process_idx):
    print(f"Printing from process {process_idx}: {state.process_index}")


def process_execution_check():
    accelerator = Accelerator()
    num_processes = accelerator.num_processes
    with accelerator.main_process_first():
        idx = torch.tensor(accelerator.process_index).to(accelerator.device)
    idxs = accelerator.gather(idx)
    assert idxs[0] == 0, "Main process was not first."

    # Test the decorators
    f = io.StringIO()
    with contextlib.redirect_stdout(f):
        accelerator.on_main_process(print_main)(accelerator.state)
    result = f.getvalue().rstrip()
    if accelerator.is_main_process:
        assert result == "Printing from the main process 0", f"{result} != Printing from the main process 0"
    else:
        assert f.getvalue().rstrip() == "", f'{result} != ""'
    f.truncate(0)
    f.seek(0)

    with contextlib.redirect_stdout(f):
        accelerator.on_local_main_process(print_local_main)(accelerator.state)
    if accelerator.is_local_main_process:
        assert f.getvalue().rstrip() == "Printing from the local main process 0"
    else:
        assert f.getvalue().rstrip() == ""
    f.truncate(0)
    f.seek(0)

    with contextlib.redirect_stdout(f):
        accelerator.on_last_process(print_last)(accelerator.state)
    if accelerator.is_last_process:
        assert f.getvalue().rstrip() == f"Printing from the last process {accelerator.state.num_processes - 1}"
    else:
        assert f.getvalue().rstrip() == ""
    f.truncate(0)
    f.seek(0)

    for process_idx in range(num_processes):
        with contextlib.redirect_stdout(f):
            accelerator.on_process(print_on, process_index=process_idx)(accelerator.state, process_idx)
        if accelerator.process_index == process_idx:
            assert f.getvalue().rstrip() == f"Printing from process {process_idx}: {accelerator.process_index}"
        else:
            assert f.getvalue().rstrip() == ""
        f.truncate(0)
        f.seek(0)


def init_state_check():
    # Test we can instantiate this twice in a row.
    state = AcceleratorState()
    if state.local_process_index == 0:
        print("Testing, testing. 1, 2, 3.")
    print(state)


def rng_sync_check():
    state = AcceleratorState()
    synchronize_rng_states(["torch"])
    assert are_the_same_tensors(torch.get_rng_state()), "RNG states improperly synchronized on CPU."
    if state.distributed_type == DistributedType.MULTI_GPU:
        synchronize_rng_states(["cuda"])
        assert are_the_same_tensors(torch.cuda.get_rng_state()), "RNG states improperly synchronized on GPU."
    generator = torch.Generator()
    synchronize_rng_states(["generator"], generator=generator)
    assert are_the_same_tensors(generator.get_state()), "RNG states improperly synchronized in generator."

    if state.local_process_index == 0:
        print("All rng are properly synched.")


def dl_preparation_check():
    state = AcceleratorState()
    length = 32 * state.num_processes

    dl = DataLoader(range(length), batch_size=8)
    dl = prepare_data_loader(dl, state.device, state.num_processes, state.process_index, put_on_device=True)
    result = []
    for batch in dl:
        result.append(gather(batch))
    result = torch.cat(result)

    print(state.process_index, result, type(dl))
    assert torch.equal(result.cpu(), torch.arange(0, length).long()), "Wrong non-shuffled dataloader result."

    dl = DataLoader(range(length), batch_size=8)
    dl = prepare_data_loader(
        dl,
        state.device,
        state.num_processes,
        state.process_index,
        put_on_device=True,
        split_batches=True,
    )
    result = []
    for batch in dl:
        result.append(gather(batch))
    result = torch.cat(result)
    assert torch.equal(result.cpu(), torch.arange(0, length).long()), "Wrong non-shuffled dataloader result."

    if state.process_index == 0:
        print("Non-shuffled dataloader passing.")

    dl = DataLoader(range(length), batch_size=8, shuffle=True)
    dl = prepare_data_loader(dl, state.device, state.num_processes, state.process_index, put_on_device=True)
    result = []
    for batch in dl:
        result.append(gather(batch))
    result = torch.cat(result).tolist()
    result.sort()
    assert result == list(range(length)), "Wrong shuffled dataloader result."

    dl = DataLoader(range(length), batch_size=8, shuffle=True)
    dl = prepare_data_loader(
        dl,
        state.device,
        state.num_processes,
        state.process_index,
        put_on_device=True,
        split_batches=True,
    )
    result = []
    for batch in dl:
        result.append(gather(batch))
    result = torch.cat(result).tolist()
    result.sort()
    assert result == list(range(length)), "Wrong shuffled dataloader result."

    if state.local_process_index == 0:
        print("Shuffled dataloader passing.")


def central_dl_preparation_check():
    state = AcceleratorState()
    length = 32 * state.num_processes

    dl = DataLoader(range(length), batch_size=8)
    dl = prepare_data_loader(
        dl, state.device, state.num_processes, state.process_index, put_on_device=True, dispatch_batches=True
    )
    result = []
    for batch in dl:
        result.append(gather(batch))
    result = torch.cat(result)
    assert torch.equal(result.cpu(), torch.arange(0, length).long()), "Wrong non-shuffled dataloader result."

    dl = DataLoader(range(length), batch_size=8)
    dl = prepare_data_loader(
        dl,
        state.device,
        state.num_processes,
        state.process_index,
        put_on_device=True,
        split_batches=True,
        dispatch_batches=True,
    )
    result = []
    for batch in dl:
        result.append(gather(batch))
    result = torch.cat(result)
    assert torch.equal(result.cpu(), torch.arange(0, length).long()), "Wrong non-shuffled dataloader result."

    if state.process_index == 0:
        print("Non-shuffled central dataloader passing.")

    dl = DataLoader(range(length), batch_size=8, shuffle=True)
    dl = prepare_data_loader(
        dl, state.device, state.num_processes, state.process_index, put_on_device=True, dispatch_batches=True
    )
    result = []
    for batch in dl:
        result.append(gather(batch))
    result = torch.cat(result).tolist()
    result.sort()
    assert result == list(range(length)), "Wrong shuffled dataloader result."

    dl = DataLoader(range(length), batch_size=8, shuffle=True)
    dl = prepare_data_loader(
        dl,
        state.device,
        state.num_processes,
        state.process_index,
        put_on_device=True,
        split_batches=True,
        dispatch_batches=True,
    )
    result = []
    for batch in dl:
        result.append(gather(batch))
    result = torch.cat(result).tolist()
    result.sort()
    assert result == list(range(length)), "Wrong shuffled dataloader result."

    if state.local_process_index == 0:
        print("Shuffled central dataloader passing.")


def mock_training(length, batch_size, generator):
    set_seed(42)
    generator.manual_seed(42)
    train_set = RegressionDataset(length=length)
    train_dl = DataLoader(train_set, batch_size=batch_size, shuffle=True, generator=generator)
    model = RegressionModel()
    optimizer = torch.optim.SGD(model.parameters(), lr=0.1)
    for epoch in range(3):
        for batch in train_dl:
            model.zero_grad()
            output = model(batch["x"])
            loss = torch.nn.functional.mse_loss(output, batch["y"])
            loss.backward()
            optimizer.step()
    return train_set, model


def training_check():
    state = AcceleratorState()
    generator = torch.Generator()
    batch_size = 8
    length = batch_size * 4 * state.num_processes

    train_set, old_model = mock_training(length, batch_size * state.num_processes, generator)
    assert are_the_same_tensors(old_model.a), "Did not obtain the same model on both processes."
    assert are_the_same_tensors(old_model.b), "Did not obtain the same model on both processes."

    accelerator = Accelerator()
    train_dl = DataLoader(train_set, batch_size=batch_size, shuffle=True, generator=generator)
    model = RegressionModel()
    optimizer = torch.optim.SGD(model.parameters(), lr=0.1)

    train_dl, model, optimizer = accelerator.prepare(train_dl, model, optimizer)
    set_seed(42)
    generator.manual_seed(42)
    for epoch in range(3):
        for batch in train_dl:
            model.zero_grad()
            output = model(batch["x"])
            loss = torch.nn.functional.mse_loss(output, batch["y"])
            accelerator.backward(loss)
            optimizer.step()

    model = accelerator.unwrap_model(model).cpu()
    assert torch.allclose(old_model.a, model.a), "Did not obtain the same model on CPU or distributed training."
    assert torch.allclose(old_model.b, model.b), "Did not obtain the same model on CPU or distributed training."

    accelerator.print("Training yielded the same results on one CPU or distributed setup with no batch split.")

    accelerator = Accelerator(split_batches=True)
    train_dl = DataLoader(train_set, batch_size=batch_size * state.num_processes, shuffle=True, generator=generator)
    model = RegressionModel()
    optimizer = torch.optim.SGD(model.parameters(), lr=0.1)

    train_dl, model, optimizer = accelerator.prepare(train_dl, model, optimizer)
    set_seed(42)
    generator.manual_seed(42)
    for _ in range(3):
        for batch in train_dl:
            model.zero_grad()
            output = model(batch["x"])
            loss = torch.nn.functional.mse_loss(output, batch["y"])
            accelerator.backward(loss)
            optimizer.step()

    model = accelerator.unwrap_model(model).cpu()
    assert torch.allclose(old_model.a, model.a), "Did not obtain the same model on CPU or distributed training."
    assert torch.allclose(old_model.b, model.b), "Did not obtain the same model on CPU or distributed training."

    accelerator.print("Training yielded the same results on one CPU or distributes setup with batch split.")

    if torch.cuda.is_available():
        # Mostly a test that FP16 doesn't crash as the operation inside the model is not converted to FP16
        print("FP16 training check.")
        AcceleratorState._reset_state()
        accelerator = Accelerator(mixed_precision="fp16")
        train_dl = DataLoader(train_set, batch_size=batch_size, shuffle=True, generator=generator)
        model = RegressionModel()
        optimizer = torch.optim.SGD(model.parameters(), lr=0.1)

        train_dl, model, optimizer = accelerator.prepare(train_dl, model, optimizer)
        set_seed(42)
        generator.manual_seed(42)
        for _ in range(3):
            for batch in train_dl:
                model.zero_grad()
                output = model(batch["x"])
                loss = torch.nn.functional.mse_loss(output, batch["y"])
                accelerator.backward(loss)
                optimizer.step()

        model = accelerator.unwrap_model(model).cpu()
        assert torch.allclose(old_model.a, model.a), "Did not obtain the same model on CPU or distributed training."
        assert torch.allclose(old_model.b, model.b), "Did not obtain the same model on CPU or distributed training."

    # BF16 support is only for CPU + TPU, and some GPU
    if is_bf16_available():
        # Mostly a test that BF16 doesn't crash as the operation inside the model is not converted to BF16
        print("BF16 training check.")
        AcceleratorState._reset_state()
        accelerator = Accelerator(mixed_precision="bf16")
        train_dl = DataLoader(train_set, batch_size=batch_size, shuffle=True, generator=generator)
        model = RegressionModel()
        optimizer = torch.optim.SGD(model.parameters(), lr=0.1)

        train_dl, model, optimizer = accelerator.prepare(train_dl, model, optimizer)
        set_seed(42)
        generator.manual_seed(42)
        for _ in range(3):
            for batch in train_dl:
                model.zero_grad()
                output = model(batch["x"])
                loss = torch.nn.functional.mse_loss(output, batch["y"])
                accelerator.backward(loss)
                optimizer.step()

        model = accelerator.unwrap_model(model).cpu()
        assert torch.allclose(old_model.a, model.a), "Did not obtain the same model on CPU or distributed training."
        assert torch.allclose(old_model.b, model.b), "Did not obtain the same model on CPU or distributed training."


def main():
    accelerator = Accelerator()
    state = accelerator.state
    if state.local_process_index == 0:
        print("**Initialization**")
    init_state_check()
    if state.local_process_index == 0:
        print("\n**Test process execution**")
    process_execution_check()

    if state.local_process_index == 0:
        print("\n**Test random number generator synchronization**")
    rng_sync_check()

    if state.local_process_index == 0:
        print("\n**DataLoader integration test**")
    dl_preparation_check()
    if state.distributed_type != DistributedType.TPU and is_torch_version(">=", "1.8.0"):
        central_dl_preparation_check()

    # Trainings are not exactly the same in DeepSpeed and CPU mode
    if state.distributed_type == DistributedType.DEEPSPEED:
        return

    if state.local_process_index == 0:
        print("\n**Training integration test**")
    training_check()


def _mp_fn(index):
    # For xla_spawn (TPUs)
    main()


if __name__ == "__main__":
    main()
