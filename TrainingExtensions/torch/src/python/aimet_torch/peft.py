# -*- mode: python -*-
# =============================================================================
#  @@-COPYRIGHT-START-@@
#
#  Copyright (c) 2024, Qualcomm Innovation Center, Inc. All rights reserved.
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are met:
#
#  1. Redistributions of source code must retain the above copyright notice,
#     this list of conditions and the following disclaimer.
#
#  2. Redistributions in binary form must reproduce the above copyright notice,
#     this list of conditions and the following disclaimer in the documentation
#     and/or other materials provided with the distribution.
#
#  3. Neither the name of the copyright holder nor the names of its contributors
#     may be used to endorse or promote products derived from this software
#     without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
#  AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
#  IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
#  ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
#  LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR
#  CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF
#  SUBSTITUTE GOODS OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS
#  INTERRUPTION) HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN
#  CONTRACT, STRICT LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE)
#  ARISING IN ANY WAY OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE
#  POSSIBILITY OF SUCH DAMAGE.
#
#  SPDX-License-Identifier: BSD-3-Clause
#
#  @@-COPYRIGHT-END-@@
# =============================================================================

""" Implementation for handling LoRA adapters added using PEFT """
from typing import Dict, Type
import os
import pickle
from collections import defaultdict
import torch.nn as nn
import torch
import onnx
from safetensors.torch import save_file
from safetensors import safe_open

# pylint: disable=import-error
# pylint: disable=no-name-in-module
from peft.tuners.lora.layer import LoraLayer as PeftLoraLayer

from aimet_torch.utils import replace_modules_of_type1_using_constructor
from aimet_torch.elementwise_ops import Add
from aimet_torch.v2.quantsim import QuantizationSimModel
from aimet_torch.quantsim import ExportableQuantModule
from aimet_torch.v2.nn import BaseQuantizationMixin
from aimet_torch.onnx_utils import OnnxSaver, get_layers_in_io_tensor_map


class LoraLayer(torch.nn.Module):
    """
    Quantizable lora layer
    """
    # pylint: disable=too-many-instance-attributes
    def __init__(self, lora_layer: PeftLoraLayer):
        """
        :param lora_layer: Lora layer we want to replace
        """
        super().__init__()
        self.base_layer = lora_layer.base_layer
        self.r = lora_layer.r
        self.lora_alpha = lora_layer.lora_alpha
        self.scaling = lora_layer.scaling
        self.lora_dropout = nn.ModuleList({})
        self.adapter_name_to_index = {}
        self.index_to_adapter_name = {}
        self.lora_A = nn.ModuleList([])
        self.lora_B = nn.ModuleList([])
        self.active_adapters = {}
        self._swap_module_dict_with_list(lora_layer)
        self.in_features = lora_layer.in_features
        self.out_features = lora_layer.out_features
        self.add_lora_to_res = Add()

    def _swap_module_dict_with_list(self, lora_layer):
        for index, adapter_name in enumerate(lora_layer.lora_A):
            self.lora_A.append(lora_layer.lora_A[adapter_name])
            self.lora_B.append(lora_layer.lora_B[adapter_name])
            self.lora_dropout.append(lora_layer.lora_dropout[adapter_name])
            self.adapter_name_to_index[adapter_name] = index
            if adapter_name in lora_layer.active_adapter:
                self.active_adapters[adapter_name] = True
            else:
                self.active_adapters[adapter_name] = False
        for adapter_name in self.adapter_name_to_index:
            self.index_to_adapter_name[self.adapter_name_to_index[adapter_name]] = adapter_name

    def forward(self, x: torch.Tensor, *args, **kwargs) -> torch.Tensor:
        """ Forward pass for replaced layer"""
        result = self.base_layer(x, *args, **kwargs)
        torch_result_dtype = result.dtype
        for active_adapter in self.active_adapters:
            if active_adapter not in self.adapter_name_to_index:
                continue
            lora_A = self.lora_A[self.adapter_name_to_index[active_adapter]]
            lora_B = self.lora_B[self.adapter_name_to_index[active_adapter]]
            dropout = self.lora_dropout[self.adapter_name_to_index[active_adapter]]
            scaling = self.scaling[active_adapter]
            x = x.to(lora_A.weight.dtype)

            result = self.add_lora_to_res(result, lora_B(lora_A(dropout(x)) * scaling))

        result = result.to(torch_result_dtype)
        return result


def replace_lora_layers_with_quantizable_layers(model: torch.nn.Module):
    """
    Utility to replace lora layers with Quantizable Lora layers

    :param model: PEFT model
    """
    replace_modules_of_type1_using_constructor(model, PeftLoraLayer, LoraLayer)


def save_lora_weights_after_adaptation(model: torch.nn.Module, path: str, filename_prefix: str):
    """
    Utility to save model weights after model adaptations

    :param model: PEFT model
    :param path: path where to store model pth and encodings
    :param filename_prefix: Prefix to use for filenames
    """
    param_to_name = {}

    for name, param in model.named_parameters():
        param_to_name[param] = name

    lora_weights = {}
    for name, module in model.named_modules():
        if isinstance(module, LoraLayer):
            for _, param in module.lora_A.named_parameters():
                name = param_to_name[param]
                lora_weights[name] = param
            for _, param in module.lora_B.named_parameters():
                name = param_to_name[param]
                lora_weights[name] = param

    filename_prefix = filename_prefix + '.safetensor'
    model_params_path = os.path.join(path, filename_prefix)
    save_file(lora_weights, model_params_path)


class AdapterMetaData:
    """
    Tracks meta data for lora layers. Tracks names of lora_a & b as well as alpha values
    Attributes:
        lora_A, lora_B, alpha
    """
    def __init__(self):
        self.lora_A = []
        self.lora_B = []
        self.alpha = None


def track_lora_meta_data(model: torch.nn.Module, path: str, filename_prefix: str,
                         replaced_module_type: Type[torch.nn.Module] = None) -> Dict[str, AdapterMetaData]:
    """
    Utility to track and save meta data for adapters. The meta data has adapter names and corresponding lora layers & alphas

    :param model: PEFT model
    :param path: path where to store model pth and encodings
    :param filename_prefix: Prefix to use for filenames
    :param replaced_module_type: If lora linear layer is replaced by another torch module, then replaced_module_type
                                represents the type with which linear layer was replaced. Otherwise pass None
    """
    module_to_name_d = {}

    for name, module in model.named_modules():
        module_to_name_d[module] = name

    adapter_name_to_meta_data = defaultdict(AdapterMetaData)
    for name, module in model.named_modules():
        if isinstance(module, LoraLayer):
            for index, lora_layer in enumerate(module.lora_A):
                if replaced_module_type and isinstance(lora_layer, replaced_module_type):
                    lora_layer = lora_layer.conv2d
                adapter_name_to_meta_data[module.index_to_adapter_name[index]].lora_A.append(
                    module_to_name_d[lora_layer])
            for index, lora_layer in enumerate(module.lora_B):
                if replaced_module_type and isinstance(lora_layer, replaced_module_type):
                    lora_layer = lora_layer.conv2d
                adapter_name_to_meta_data[module.index_to_adapter_name[index]].lora_B.append(
                    module_to_name_d[lora_layer])
            for lora_adapter_name in module.lora_alpha:
                adapter_name_to_meta_data[lora_adapter_name].alpha = module.lora_alpha[lora_adapter_name]


    file_name = os.path.join(path, f"{filename_prefix}.pkl")
    with open(file_name, 'wb') as file:
        pickle.dump(adapter_name_to_meta_data, file)
    return adapter_name_to_meta_data


class PeftQuantUtils:
    """
    Utilities for quantizing peft model
    """
    def __init__(self, adapater_name_to_meta_data: Dict[str, AdapterMetaData], name_to_module_dict=None):
        """
        Init for Peft utilities for quantization

        :param adapater_name_to_meta_data: Dict mapping adapter name to meta data. Output of track_meta_data
        :param name_to_module_dict: PT Name to module prepared model name mapping
        """
        self.adapter_name_to_meta_data = adapater_name_to_meta_data
        self.lora_layers = self._get_lora_layers()
        self.pt_name_to_prepared_name, self.prepared_name_to_pt_name = None, None
        self.pt_to_lora_name = dict.fromkeys(self.lora_layers, '')
        if name_to_module_dict:
            self.pt_name_to_prepared_name, self.prepared_name_to_pt_name = self._get_pytorch_name_to_prepared_name(name_to_module_dict)
            self.lora_to_pt_name, self.pt_to_lora_name = self._get_lora_name_to_pytorch_name()

    @staticmethod
    def _get_pytorch_name_to_prepared_name(name_to_module_dict):
        """
        Gets onnx names to pytorch names mapping and vice versa

        :param model: PT model
        """
        pt_name_to_onnx_name = {}
        onnx_name_to_pt_name = {}
        for pytorch_name in name_to_module_dict:
            onnx_name = name_to_module_dict[pytorch_name][0]
            pt_name_to_onnx_name[pytorch_name] = onnx_name
            onnx_name_to_pt_name[onnx_name] = pytorch_name
        return pt_name_to_onnx_name, onnx_name_to_pt_name

    def _get_lora_name_to_pytorch_name(self):
        """
        Gets most similar pytorch name for every lora name
        """
        lora_to_pytorch_name = {}
        pytorch_to_lora_name = {}
        for pt_name in self.pt_name_to_prepared_name:
            for lora_name in self.lora_layers:
                if pt_name in lora_name:
                    lora_to_pytorch_name[lora_name] = pt_name
                    pytorch_to_lora_name[pt_name] = lora_name
        return lora_to_pytorch_name, pytorch_to_lora_name

    def _get_lora_layers(self) -> set:
        """
        Gets all lora layers
        """
        lora_layers = set()
        for adapter_name in self.adapter_name_to_meta_data:
            for lora_module in self.adapter_name_to_meta_data[adapter_name].lora_A:
                lora_layers.add(lora_module)
            for lora_module in self.adapter_name_to_meta_data[adapter_name].lora_B:
                lora_layers.add(lora_module)
        return lora_layers

    @staticmethod
    def _freeze_quantizer(quantizer):
        """
        Disables compute encodings and gradient update for a quantizer

        :param quantizer: Param, output or Input quantizer
        """
        # pylint:disable = protected-access
        quantizer._allow_overwrite = False
        quantizer.requires_grad_(False)

    def freeze_base_model_param_quantizers(self, sim: QuantizationSimModel):
        """
        Freeze parameter quantizers of base model

        :param sim: QuantSim model
        """
        for module_name, module in sim.model.named_modules():
            if self.prepared_name_to_pt_name and module_name in self.prepared_name_to_pt_name:
                module_name = self.prepared_name_to_pt_name[module_name]
            if isinstance(module, BaseQuantizationMixin) and module_name not in self.pt_to_lora_name:
                for _, param_quantizer in module.param_quantizers.items():
                    if param_quantizer:
                        self._freeze_quantizer(param_quantizer)

    def freeze_base_model_activation_quantizers(self, sim: QuantizationSimModel):
        """
        Freeze activation quantizers of base model

        :param sim: QuantSim model
        """
        for module_name, module in sim.model.named_modules():
            if self.prepared_name_to_pt_name and module_name in self.prepared_name_to_pt_name:
                module_name = self.prepared_name_to_pt_name[module_name]
            if isinstance(module, BaseQuantizationMixin) and module_name not in self.pt_to_lora_name:
                for input_quantizer, output_quantizer in zip(module.input_quantizers, module.output_quantizers):
                    if input_quantizer:
                        self._freeze_quantizer(input_quantizer)
                    if output_quantizer:
                        self._freeze_quantizer(output_quantizer)

    def freeze_base_model(self, sim: QuantizationSimModel):
        """
        Freeze entire base model

        :param sim: QuantSim model
        """
        self.freeze_base_model_activation_quantizers(sim)
        self.freeze_base_model_param_quantizers(sim)

    def set_bitwidth_for_lora_adapters(self, sim: QuantizationSimModel,
                                       output_bw: int, param_bw: int):
        """
        Sets output and param bitwidth for all Lora adapters added to the model

        :param sim: QuantSim model
        :param output_bw: Output BW
        :param param_bw: Parameter BW
        """
        for module_name, module in sim.model.named_modules():
            if self.prepared_name_to_pt_name and module_name in self.prepared_name_to_pt_name:
                module_name = self.prepared_name_to_pt_name[module_name]
            if isinstance(module, BaseQuantizationMixin) and module_name in self.pt_to_lora_name:
                self._set_bitwidth_for_module(module, output_bw, param_bw)

    def get_quantized_lora_layer(self, sim: QuantizationSimModel):
        """
        This function can be used to generate lora quantized layers
        Use cases: 1) New quantizers can be created and assigned to lora quantized layer.
                   New quantizers may be required if changing - Changing dtype, per channel to per tensor
                   and vice versa
                   2) Assign new values to symmetric, bitwidth

        :param sim: QuantSim model
        """
        for module_name, module in sim.model.named_modules():
            if self.prepared_name_to_pt_name and module_name in self.prepared_name_to_pt_name:
                module_name = self.prepared_name_to_pt_name[module_name]
            if isinstance(module, BaseQuantizationMixin) and module_name in self.pt_to_lora_name:
                yield module_name, module

    @staticmethod
    def _set_bitwidth_for_module(module: BaseQuantizationMixin, output_bw: int, param_bw: int):
        """
        Sets bitwidth for a QcQuantizeWrapper module

        :param module: QcQuantize wrapper module
        :param output_bw: Output BW
        :param param_bw: Parameter BW
        """
        for output_quantizer in module.output_quantizers:
            output_quantizer.bitwidth = output_bw
        for _, param_quantizer in module.param_quantizers.items():
            param_quantizer.bitwidth = param_bw

    def export_adapter_weights(self, sim: QuantizationSimModel, path: str, filename_prefix: str, onnx_model_path: str):
        """
        Exports adapter weights to safetensor format

        :param sim: QuantSim model
        :param path: path where to store model pth and encodings
        :param filename_prefix: Prefix to use for filenames of the model pth and encodings files
        :param onnx_model_path: Path from where we can load the exported onnx model. This can be the same path to where
                                QuantSim exported the ONNX model
        """
        # pylint: disable=too-many-locals
        assert os.path.exists(onnx_model_path), 'The onnx model does not exist in the location specified'

        onnx_model = onnx.load(onnx_model_path)
        onnx_node_to_io_tensor_map, _ = OnnxSaver.get_onnx_node_to_io_tensor_names_map(onnx_model)
        layers_to_onnx_op_names = get_layers_in_io_tensor_map(onnx_node_to_io_tensor_map)

        tensors = {}

        for module_name, module in sim.model.named_modules():
            if not isinstance(module, ExportableQuantModule):
                continue

            if module_name in layers_to_onnx_op_names:
                onnx_name = layers_to_onnx_op_names[module_name][0]
                if self.prepared_name_to_pt_name and module_name in self.prepared_name_to_pt_name:
                    pt_name = self.prepared_name_to_pt_name[module_name]
                    if pt_name in self.pt_to_lora_name:
                        module_name = self.pt_to_lora_name[pt_name]
                if module_name in self.lora_layers:
                    for param_name, param in module.named_parameters():
                        if param_name in ['weight', 'bias']:
                            tensor_name = onnx_name + '.' + param_name
                            tensors[tensor_name] = param
        filename_prefix = filename_prefix + '.safetensor'
        model_params_path = os.path.join(path, filename_prefix)
        save_file(tensors, model_params_path)

    def enable_adapter_and_load_weights(self, sim: QuantizationSimModel, adapter_weights_path,
                                        use_safetensor: bool = True):
        """
        Enables adapter effect on base model by loading weights to model

        :param sim: QuantSim model
        :param adapter_weights_path: Path to adapter weights (adapter weights should be either bin file or safetensor)
        :param use_safetensor: True if adapter weights path point to a safetensor file. False if points to bin file
        """
        tensors = {}
        if use_safetensor:
            with safe_open(adapter_weights_path, framework="pt", device=0) as f:
                for key in f.keys():
                    tensors[key] = f.get_tensor(key)
        else:
            tensors = torch.load(adapter_weights_path)

        onnx_names_tensors = {}
        for key in tensors.keys():
            tensor_name = key
            if self.prepared_name_to_pt_name:
                temp_key = key[0:key.find('.weight')]
                tensor_name = self.pt_name_to_prepared_name[self.lora_to_pt_name[temp_key]] + '.weight'
            onnx_names_tensors[tensor_name] = tensors[key]

        sim.model.load_state_dict(onnx_names_tensors, strict=False)

    def disable_lora_adapters(self, sim: QuantizationSimModel):
        """
        Disables adapter (zero out weights for lora A & B) effect on base model by loading weights to model

        :param sim: QuantSim model
        """
        tensors = {}
        for module_name, module in sim.model.named_modules():
            org_name = module_name
            if self.prepared_name_to_pt_name and module_name in self.prepared_name_to_pt_name:
                pt_name = self.prepared_name_to_pt_name[module_name]
                if pt_name in self.pt_to_lora_name:
                    module_name = self.pt_to_lora_name[pt_name]
            if module_name in self.lora_layers:
                for param_name, param in module.named_parameters():
                    if param_name in ['weight', 'bias']:
                        tensor_name = org_name + '.' + param_name
                        tensors[tensor_name] = torch.zeros_like(param)

        sim.model.load_state_dict(tensors, strict=False)
