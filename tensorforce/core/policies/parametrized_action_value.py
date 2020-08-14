# Copyright 2020 Tensorforce Team. All Rights Reserved.
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
# ==============================================================================

import tensorflow as tf

from tensorforce import TensorforceError
from tensorforce.core import distribution_modules, layer_modules, ModuleDict, network_modules, \
    TensorDict, tf_function, tf_util
from tensorforce.core.policies import ActionValue


class ParametrizedActionValue(ActionValue):
    """
    Policy which parametrizes independent value functions per action, conditioned on the output of a
    central neural network processing the input state
    (specification key: `parametrized_action_value`).

    Args:
        network ('auto' | specification): Policy network configuration, see
            [networks](../modules/networks.html)
            (<span style="color:#00C000"><b>default</b></span>: 'auto', automatically configured
            network).
        device (string): Device name
            (<span style="color:#00C000"><b>default</b></span>: inherit value of parent module).
        l2_regularization (float >= 0.0): Scalar controlling L2 regularization
            (<span style="color:#00C000"><b>default</b></span>: inherit value of parent module).
        name (string): <span style="color:#0000C0"><b>internal use</b></span>.
        states_spec (specification): <span style="color:#0000C0"><b>internal use</b></span>.
        auxiliaries_spec (specification): <span style="color:#0000C0"><b>internal use</b></span>.
        internals_spec (specification): <span style="color:#0000C0"><b>internal use</b></span>.
        actions_spec (specification): <span style="color:#0000C0"><b>internal use</b></span>.
    """

    # Network first
    def __init__(
        self, network='auto', *, device=None, l2_regularization=None, name=None, states_spec=None,
        auxiliaries_spec=None, internals_spec=None, actions_spec=None
    ):
        super().__init__(
            device=device, l2_regularization=l2_regularization, name=name, states_spec=states_spec,
            auxiliaries_spec=auxiliaries_spec, actions_spec=actions_spec
        )

        if not all(spec.type in ('bool', 'int') for spec in self.actions_spec.values()):
            raise TensorforceError.value(
                name='ParametrizedActionValue', argument='actions_spec', value=actions_spec,
                hint='types not bool/int'
            )

        # Network
        self.network = self.submodule(
            name='network', module=network, modules=network_modules, inputs_spec=self.states_spec
        )
        output_spec = self.network.output_spec()
        if output_spec.type != 'float':
            raise TensorforceError.type(
                name='ParametrizedDistributions', argument='network output', dtype=output_spec.type
            )

        # Action values
        def function(name, spec):
            if name is None:
                name = 'value'
            else:
                name = name + '_value'
            if spec.type == 'bool':
                return self.submodule(
                    name=name, module='linear', modules=layer_modules, size=(spec.size * 2),
                    input_spec=output_spec
                )
            elif spec.type == 'int':
                return self.submodule(
                    name=name, module='linear', modules=layer_modules,
                    size=(spec.size * spec.num_values), input_spec=output_spec
                )

        self.values = self.actions_spec.fmap(function=function, cls=ModuleDict, with_names=True)

    @property
    def internals_spec(self):
        return self.network.internals_spec

    def internals_init(self):
        return self.network.internals_init()

    def max_past_horizon(self, *, on_policy):
        return self.network.max_past_horizon(on_policy=on_policy)

    @tf_function(num_args=0)
    def past_horizon(self, *, on_policy):
        return self.network.past_horizon(on_policy=on_policy)

    @tf_function(num_args=4)
    def act(self, *, states, horizons, internals, auxiliaries, independent):
        embedding, internals = self.network.apply(
            x=states, horizons=horizons, internals=internals, independent=independent
        )

        def function(name, spec, value_layer):
            action_value = value_layer.apply(x=embedding)
            if spec.type == 'bool':
                shape = (-1,) + (spec.size,) + (2,)
                action_value = tf.reshape(tensor=action_value, shape=shape)
                action = (action_value[:, :, 0] > action_value[:, :, 1])
                action = tf.reshape(tensor=action, shape=((-1,) + spec.shape))
            elif spec.type == 'int':
                shape = (-1,) + spec.shape + (spec.num_values,)
                action_value = tf.reshape(tensor=action_value, shape=shape)
                mask = auxiliaries[name]['mask']
                min_float = tf_util.get_dtype(type='float').min
                min_float = tf.fill(dims=tf.shape(input=action_value), value=min_float)
                action_value = tf.where(condition=mask, x=action_value, y=min_float)
                action = tf.math.argmax(input=action_value, axis=-1, output_type=spec.tf_type())
            return action

        actions = self.actions_spec.fmap(
            function=function, cls=TensorDict, zip_values=(self.values,), with_names=True
        )

        return actions, internals

    @tf_function(num_args=5)
    def action_values(self, *, states, horizons, internals, auxiliaries, actions):
        embedding, _ = self.network.apply(
            x=states, horizons=horizons, internals=internals, independent=True
        )

        def function(spec, value_layer, action):
            action_value = value_layer.apply(x=embedding)
            if spec.type == 'bool':
                shape = (-1,) + spec.shape + (2,)
                action_value = tf.reshape(tensor=action_value, shape=shape)
                action_value = tf.where(
                    condition=action, x=action_value[..., 0], y=action_value[..., 1]
                )
            elif spec.type == 'int':
                shape = (-1,) + spec.shape + (spec.num_values,)
                action_value = tf.reshape(tensor=action_value, shape=shape)
                rank = spec.rank + 1
                action = tf.expand_dims(input=action, axis=rank)
                action_value = tf.gather(params=action_value, indices=action, batch_dims=rank)
                action_value = tf.squeeze(input=action_value, axis=rank)
            return action_value

        return self.actions_spec.fmap(
            function=function, cls=TensorDict, zip_values=(self.values, actions)
        )

    @tf_function(num_args=4)
    def state_values(self, *, states, horizons, internals, auxiliaries):
        embedding, _ = self.network.apply(
            x=states, horizons=horizons, internals=internals, independent=True
        )

        def function(name, spec, value_layer):
            action_value = value_layer.apply(x=embedding)
            if spec.type == 'bool':
                shape = (-1,) + (spec.size,) + (2,)
                action_value = tf.reshape(tensor=action_value, shape=shape)
                state_value = tf.math.maximum(x=action_value[:, :, 0], y=action_value[:, :, 1])
                state_value = tf.reshape(tensor=state_value, shape=((-1,) + spec.shape))
            elif spec.type == 'int':
                shape = (-1,) + spec.shape + (spec.num_values,)
                action_value = tf.reshape(tensor=action_value, shape=shape)
                mask = auxiliaries[name]['mask']
                min_float = tf_util.get_dtype(type='float').min
                min_float = tf.fill(dims=tf.shape(input=action_value), value=min_float)
                action_value = tf.where(condition=mask, x=action_value, y=min_float)
                state_value = tf.math.reduce_max(input_tensor=action_value, axis=-1)
            return state_value

        return self.actions_spec.fmap(
            function=function, cls=TensorDict, zip_values=(self.values,), with_names=True
        )