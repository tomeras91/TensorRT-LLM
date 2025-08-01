import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Generic, List, Optional, TypeVar

import torch
import transformers

from tensorrt_llm import logger
from tensorrt_llm._torch.pyexecutor.config_utils import is_nemotron_hybrid
from tensorrt_llm._utils import torch_dtype_to_binding
from tensorrt_llm.bindings import LayerType as LayerTypeCpp
from tensorrt_llm.functional import AllReduceStrategy
from tensorrt_llm.logger import logger
from tensorrt_llm.mapping import Mapping
from tensorrt_llm.models.modeling_utils import QuantConfig
from tensorrt_llm.quantization.mode import QuantAlgo

TConfig = TypeVar("TConfig", bound=transformers.PretrainedConfig)


@dataclass
class MoeLoadBalancerConfig:
    num_slots: Optional[int] = None
    initial_global_assignments: Optional[Dict[int,
                                              List[int]]] = field(default=None,
                                                                  repr=False)
    layer_updates_per_iter: int = 0

    ep_rank: Optional[int] = field(default=None, init=False)
    ep_size: Optional[int] = field(default=None, init=False)

    def setup(self, ep_rank: int, ep_size: int) -> None:
        self.ep_rank = ep_rank
        self.ep_size = ep_size
        assert self.num_slots is not None

    @property
    def num_local_slots(self) -> int:
        return self.num_slots // self.ep_size

    @property
    def slot_start(self) -> int:
        return self.ep_rank * self.num_local_slots

    @property
    def slot_end(self) -> int:
        return self.slot_start + self.num_local_slots

    def get_layer_initial_global_assignments(self, layer_idx: int) -> List[int]:
        if self.initial_global_assignments is not None:
            assert layer_idx in self.initial_global_assignments
            assert len(
                self.initial_global_assignments[layer_idx]) == self.num_slots
            return self.initial_global_assignments[layer_idx]
        else:
            return None


@dataclass(kw_only=True)
class ModelConfig(Generic[TConfig]):
    pretrained_config: Optional[TConfig] = None
    mapping: Mapping = field(default_factory=Mapping)

    # quantization configs
    quant_config: QuantConfig = field(default_factory=QuantConfig)
    # TODO(qijun): support per linear layer quantization
    quant_config_dict: Optional[Dict[str, QuantConfig]] = None
    # Delay weights creation to DecoderModelForCausalLM.__post_init__
    # to support mixed quantization.
    skip_create_weights_in_init: bool = False

    spec_config: Optional["DecodingBaseConfig"] = None
    lora_config: Optional["LoraConfig"] = None

    is_generation: bool = True
    max_num_tokens: int = 8192
    max_seq_len: Optional[int] = None

    moe_max_num_tokens: Optional[int] = None
    moe_load_balancer: Optional[MoeLoadBalancerConfig] = None

    attn_backend: str = 'TRTLLM'
    moe_backend: str = 'CUTLASS'  # options can be CUTLASS, TRTLLM
    allreduce_strategy: AllReduceStrategy = AllReduceStrategy.AUTO

    # If true, enable min-latency mode. Currently only used for Llama4.
    enable_min_latency: bool = False

    # Allow models to select op according to whether CUDA Graphs are used.
    use_cuda_graph: bool = False

    force_dynamic_quantization: bool = False

    extra_attrs: Dict = field(default_factory=dict, repr=False, init=False)

    _frozen: bool = field(default=False, init=False, repr=False)

    def __setattr__(self, key, value):
        """
        Prevent modification of frozen instance attributes.
        However, we allow modification of 'extra_attrs' attributes for torch.compile
        and 'pretrained_config' attributes for mutimodal models. All the other
        attributes are frozen.
        This can be bypassed by manually setting '_frozen' to False. The design is
        to discourage modifying the attributes unintentionally.
        """
        if self._frozen:
            if key not in ('_frozen', 'extra_attrs', 'pretrained_config'):
                raise AttributeError(
                    f"Cannot modify ModelConfig.'{key}' - instance is frozen")
        super().__setattr__(key, value)

    def __post_init__(self):
        if self.pretrained_config and hasattr(self.pretrained_config,
                                              "architectures"):
            self.is_generation = self.is_generation_model(
                self.pretrained_config.architectures)

        def get_all_reduce_strategy(strategy: str = "AUTO"):
            maps = {
                "AUTO": AllReduceStrategy.AUTO,
                "NCCL": AllReduceStrategy.NCCL,
                "UB": AllReduceStrategy.UB,
                "MINLATENCY": AllReduceStrategy.MIN_LATENCY,
                "ONESHOT": AllReduceStrategy.ONESHOT,
                "TWOSHOT": AllReduceStrategy.TWOSHOT,
                "LOWPRECISION": AllReduceStrategy.LOWPRECISION,
                "MNNVL": AllReduceStrategy.MNNVL
            }
            key = strategy.upper()
            return maps[key] if key in maps else AllReduceStrategy.AUTO

        if isinstance(self.allreduce_strategy, str):
            self.allreduce_strategy = get_all_reduce_strategy(
                self.allreduce_strategy)

    @property
    def fuse_pos_embd(self):
        if self.attn_backend == 'TRTLLM':
            return True
        elif self.attn_backend == 'FLASHINFER':
            return False
        return False

    @property
    def enable_flash_mla(self):
        if self.attn_backend == 'TRTLLM':
            if hasattr(self.pretrained_config, "kv_lora_rank") and hasattr(
                    self.pretrained_config, "qk_rope_head_dim"):
                head_dim = self.pretrained_config.kv_lora_rank + self.pretrained_config.qk_rope_head_dim
                if head_dim == 576 and torch.cuda.get_device_capability() == (
                        9, 0):
                    return True
        return False

    def get_quant_config(self, name: Optional[str] = None) -> QuantConfig:
        if name is None or self.per_layer_quant_configs is None:
            return self.quant_config

        if name in self.per_layer_quant_configs:
            return self.per_layer_quant_configs[name]

        raise ValueError(f'quant config of {name} is not found')

    @staticmethod
    def is_generation_model(model_architectures: Optional[List[str]]) -> bool:
        if model_architectures is None:
            logger.warning(
                "Model architectures is None, default to is_generation_model=True"
            )
            return True
        return model_architectures[0] not in [
            "BertForSequenceClassification", "Qwen2ForProcessRewardModel",
            "Qwen2ForRewardModel", "LlamaForTextEmbedding"
        ]
        # TODO: should be 'not model_type == ModelType.ENCODER_ONLY'
        # once ModelType is used in pytorch flow.

    @classmethod
    def from_pretrained(cls,
                        checkpoint_dir: str,
                        trust_remote_code=False,
                        **kwargs):
        pretrained_config = transformers.AutoConfig.from_pretrained(
            checkpoint_dir,
            trust_remote_code=trust_remote_code,
        )

        # Find the cache path by looking for the config.json file which should be in all
        # huggingface models
        model_dir = Path(
            transformers.utils.hub.cached_file(checkpoint_dir,
                                               'config.json')).parent
        quant_config = QuantConfig()
        layer_quant_config = None
        # quantized ckpt in modelopt format
        quant_config_file = model_dir / 'hf_quant_config.json'
        if quant_config_file.exists():
            with open(quant_config_file) as f:
                quant_config_dict = json.load(f)

            json_quant_configs = quant_config_dict['quantization']

            quant_config.quant_algo = json_quant_configs.get('quant_algo', None)
            # fp8_pb_wo from modelopt is the same as FP8_BLOCK_SCALES
            if quant_config.quant_algo == "fp8_pb_wo":
                quant_config.quant_algo = 'FP8_BLOCK_SCALES'
            quant_config.kv_cache_quant_algo = json_quant_configs.get(
                'kv_cache_quant_algo', None)
            quant_config.group_size = json_quant_configs.get('group_size', None)
            quant_config.exclude_modules = json_quant_configs.get(
                'exclude_modules', None)

            if quant_config.quant_algo == QuantAlgo.MIXED_PRECISION:
                mixed_quant_config_file = model_dir / 'quant_cfg.json'
                with open(mixed_quant_config_file) as fm:
                    mixed_quant_configs = json.load(fm)
                    # kv_cache_quant_algo is global regardless of MIXED_PRECISION
                    kv_cache_quant_algo = mixed_quant_configs[
                        'kv_cache_quant_algo']
                    mixed_quant_configs = mixed_quant_configs[
                        'quantized_layers']
                    if kv_cache_quant_algo is not None and quant_config.kv_cache_quant_algo is not None:
                        if kv_cache_quant_algo != quant_config.kv_cache_quant_algo:
                            raise RuntimeError(
                                f"The kvcache config in 'quant_cfg.json', {kv_cache_quant_algo},"
                                f"is different from 'hf_quant_config.json', {quant_config.kv_cache_quant_algo}!"
                            )
                    kv_cache_quant_algo = kv_cache_quant_algo or quant_config.kv_cache_quant_algo

                    for layer in mixed_quant_configs:
                        config = QuantConfig()
                        config.kv_cache_quant_algo = kv_cache_quant_algo
                        config.quant_algo = mixed_quant_configs[layer][
                            'quant_algo']
                        config.group_size = mixed_quant_configs[layer].get(
                            'group_size', None)
                        mixed_quant_configs[layer] = config
                layer_quant_config = mixed_quant_configs
            elif quant_config.quant_algo == QuantAlgo.FP8_BLOCK_SCALES:
                if quant_config.group_size is None:
                    quant_config.group_size = 128

            if kwargs.get(
                    'moe_backend'
            ) == 'TRTLLM' and quant_config.quant_algo == "FP8_BLOCK_SCALES" and quant_config.exclude_modules is None:
                quant_config.exclude_modules = [
                    "*kv_b_proj*", "*k_b_proj*", "*eh_proj"
                ]

        # quantized ckpt in other formats
        elif hasattr(pretrained_config, "quantization_config"):
            hf_quant_config = pretrained_config.quantization_config
            # DeepSeek V3 FP8 ckpt
            if hf_quant_config.get(
                    "quant_method") == "fp8" and hf_quant_config.get(
                        "weight_block_size", []):
                quant_config.quant_algo = QuantAlgo.FP8_BLOCK_SCALES
                if kwargs.get('moe_backend') == 'TRTLLM':
                    # TODO: This is a hack. Remove after fp8 bmm is integrated.
                    quant_config.exclude_modules = [
                        "*kv_b_proj*", "*k_b_proj*", "*eh_proj"
                    ]
                else:
                    quant_config.exclude_modules = ["*eh_proj"]

                block_size = hf_quant_config.get("weight_block_size", [])
                assert tuple(block_size) == (
                    128,
                    128), "FP8_BLOCK_SCALES only supports block_size=(128,128)"
                quant_config.group_size = block_size[0]

        model_config = cls(pretrained_config=pretrained_config,
                           quant_config=quant_config,
                           quant_config_dict=layer_quant_config,
                           **kwargs)
        model_config._frozen = True
        return model_config

    def get_bindings_model_config(self,
                                  tokens_per_block: Optional[int] = None
                                  ) -> "ModelConfigCpp":
        """
        This method is used to construct the bindings config for the model.
        Currently it adheres to gptJsonConfig.cpp::createModelConfig, which assumes
        that an engine has been created.

        Args:
            tokens_per_block: The number of tokens per block. Please note that in PyTorch flow tokens_per_block is not available in the model config, instead it is defined in the executor config.

        Returns:
            The bindings model config.
        """
        # TODO smor- this isn't robust, and currently tested for LlamaConfig only
        # TODO smor- currently assuming no rnn layers, no MOE
        from tensorrt_llm.bindings import ModelConfig as ModelConfigCpp

        num_heads = self.pretrained_config.num_attention_heads // (
            self.mapping.tp_size * self.mapping.cp_size)

        # Handle both uniform and per-layer KV heads
        num_kv_heads_per_layer = getattr(self.pretrained_config,
                                         'num_kv_heads_per_layer', None)
        if num_kv_heads_per_layer is not None:
            # For models with per-layer KV heads, like nemotron-nas
            kv_heads_per_layer_raw = num_kv_heads_per_layer
            use_per_layer_kv_heads = True
        else:
            # Check if num_key_value_heads is a list (per-layer) or scalar (uniform)
            num_kv_heads_raw = getattr(self.pretrained_config,
                                       'num_key_value_heads', None)

            if num_kv_heads_raw is not None and isinstance(
                    num_kv_heads_raw, list):
                # num_key_value_heads is a list - treat as per-layer KV heads
                kv_heads_per_layer_raw = num_kv_heads_raw
                use_per_layer_kv_heads = True
            else:
                # num_key_value_heads is scalar or None - treat as uniform KV heads
                if num_kv_heads_raw is None:
                    # For uniform models, check: num_key_value_heads (standard) -> num_query_groups (NeMo) -> num_attention_heads
                    num_kv_heads_raw = getattr(
                        self.pretrained_config, 'num_query_groups',
                        self.pretrained_config.num_attention_heads)

                num_kv_heads = num_kv_heads_raw // (self.mapping.tp_size *
                                                    self.mapping.cp_size)
                use_per_layer_kv_heads = False

        if use_per_layer_kv_heads:
            # TRT-LLM LoRA requires uniform KV heads across layers
            if self.lora_config is not None and len(
                    set(kv_heads_per_layer_raw)) > 1:
                raise ValueError(
                    f"TRT-LLM LoRA requires uniform KV heads across layers, "
                    f"got: {kv_heads_per_layer_raw}")
            # Apply TP/CP scaling to each layer
            num_kv_heads_per_layer = [
                kv_heads // (self.mapping.tp_size * self.mapping.cp_size)
                for kv_heads in kv_heads_per_layer_raw
            ]

        hidden_size = self.pretrained_config.hidden_size // self.mapping.tp_size

        model_config_cpp = ModelConfigCpp(
            vocab_size=self.pretrained_config.vocab_size,
            num_layers=self.pretrained_config.num_hidden_layers,
            num_attention_layers=self.get_num_attention_layers(),
            num_rnn_layers=0,
            num_heads=num_heads,
            hidden_size=hidden_size,
            data_type=torch_dtype_to_binding(
                self.pretrained_config.torch_dtype))

        # For kv cache size calculation: set tokens_per_block
        if tokens_per_block is None:
            logger.warning(
                f"tokens_per_block is not set, using default value {model_config_cpp.tokens_per_block}"
            )
        else:
            model_config_cpp.tokens_per_block = tokens_per_block

        if use_per_layer_kv_heads:
            model_config_cpp.num_kv_heads_per_layer = num_kv_heads_per_layer
        else:
            model_config_cpp.set_num_kv_heads(num_kv_heads)

        mlp_hidden_size = None
        if self.pretrained_config.intermediate_size is not None:
            mlp_hidden_size = self.pretrained_config.intermediate_size // self.mapping.tp_size
        else:
            # TODO: once tensorrt_llm._torch.AutoConfig is implemented, the following logic
            # should be moved to tensorrt_llm._torch.AutoConfig of the relevant modeling_xxx file
            if hasattr(self.pretrained_config, "architectures"
                       ) and self.pretrained_config.architectures is not None:
                architectures = self.pretrained_config.architectures
                if len(architectures
                       ) == 1 and architectures[0] == "DeciLMForCausalLM":
                    mlp_hidden_size = self._infer_nemotron_ffn_mult()
                else:
                    raise ValueError(
                        f"Inferring mlp hidden size for model architecture: {architectures} isn't supported yet"
                    )
        if mlp_hidden_size is None:
            raise ValueError(
                f"Failed to infer mlp hidden size for model: {self.pretrained_config.model_type}"
            )

        # For kv cache size calculation: set size_per_head
        head_dim_names = ["head_size", "head_dim"]
        for head_dim_name in head_dim_names:
            if head_dim_name in self.pretrained_config:
                head_size = getattr(self.pretrained_config, head_dim_name)
                break
        else:
            logger.warning(
                f"head_size/head_dim is not set, using default value {hidden_size // num_heads}"
            )
            head_size = hidden_size // num_heads

        model_config_cpp.mlp_hidden_size = mlp_hidden_size
        model_config_cpp.size_per_head = head_size

        # NOTE: this method is not robust, for Gemma3ForCausalLM only
        layer_types = self.get_layer_types()
        if layer_types is not None:
            model_config_cpp.layer_types = layer_types

        return model_config_cpp

    def _infer_nemotron_ffn_mult(self):
        # TODO smor: this is a hack to support Nemotron-Super-49B-v1 with LoRA, tracked by TRTLLM-5045 ticket
        # Nemotron-NAS has variable ffn_mult for each layer, we need to find the maximum
        # so that we don't set a too small mlp_hidden_size. This solution leads to a memory
        # consumption that is higher than required.
        biggest_ffn_mult = max([
            (x.ffn.ffn_mult if x.ffn.ffn_mult is not None else 0)
            for x in self.pretrained_config.block_configs
        ])

        from tensorrt_llm._torch.models.modeling_nemotron_nas import \
            _ffn_mult_to_intermediate_size
        mlp_hidden_size = _ffn_mult_to_intermediate_size(
            biggest_ffn_mult, self.pretrained_config.hidden_size)

        return mlp_hidden_size

    def get_layer_types(self) -> Optional[List[LayerTypeCpp]]:
        """
        This method is a hack to support the effort to switch to KvCacheManagerCpp.
        Currently, it is only tested for Gemma3ForCausalLM. For other models, it will return None.
        """
        if self.pretrained_config.architectures[0] in ["Gemma3ForCausalLM"]:
            logger.debug(
                f"Setting layer types for {self.pretrained_config.architectures}"
            )
            return [
                LayerTypeCpp.ATTENTION,
            ] * self.pretrained_config.num_hidden_layers
        else:
            return None

    def get_num_attention_layers(self):
        if is_nemotron_hybrid(self.pretrained_config):
            return self.pretrained_config.hybrid_override_pattern.count("*")
        else:
            return self.pretrained_config.num_hidden_layers
