from __future__ import annotations

from collections import OrderedDict

from torch import nn
from transformers import Wav2Vec2Model, Wav2Vec2PreTrainedModel
from transformers.modeling_outputs import CausalLMOutput


class LyricWav2Vec2ForCTC(Wav2Vec2PreTrainedModel):
    """Inference-compatible architecture for nguyenvulebinh/lyric-alignment.

    That checkpoint was trained with a three-layer feature transform between
    Wav2Vec2 and the CTC head. AutoModelForCTC silently ignores those weights,
    so loading it as a stock Wav2Vec2ForCTC produces unusable posteriors.
    """

    def __init__(self, config):
        super().__init__(config)
        self.wav2vec2 = Wav2Vec2Model(config)
        self.dropout = nn.Dropout(config.final_dropout)
        self.feature_transform = nn.Sequential(
            OrderedDict(
                [
                    ("linear1", nn.Linear(config.hidden_size, config.hidden_size)),
                    ("bn1", nn.BatchNorm1d(config.hidden_size)),
                    ("activation1", nn.LeakyReLU()),
                    ("drop1", nn.Dropout(config.final_dropout)),
                    ("linear2", nn.Linear(config.hidden_size, config.hidden_size)),
                    ("bn2", nn.BatchNorm1d(config.hidden_size)),
                    ("activation2", nn.LeakyReLU()),
                    ("drop2", nn.Dropout(config.final_dropout)),
                    ("linear3", nn.Linear(config.hidden_size, config.hidden_size)),
                    ("bn3", nn.BatchNorm1d(config.hidden_size)),
                    ("activation3", nn.LeakyReLU()),
                    ("drop3", nn.Dropout(config.final_dropout)),
                ]
            )
        )
        self.lm_head = nn.Linear(config.hidden_size, config.vocab_size)
        self.post_init()

    def forward(
        self,
        input_values,
        attention_mask=None,
        output_attentions=None,
        output_hidden_states=None,
        return_dict=None,
        **_unused,
    ):
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        outputs = self.wav2vec2(
            input_values,
            attention_mask=attention_mask,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )
        hidden_states = self.dropout(outputs[0])
        batch, frames, features = hidden_states.shape
        hidden_states = self.feature_transform(hidden_states.reshape(batch * frames, features))
        logits = self.lm_head(hidden_states.reshape(batch, frames, features))
        if not return_dict:
            return (logits, *outputs[2:])
        return CausalLMOutput(
            logits=logits,
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )
