# Copyright (c) 2016-present, Facebook, Inc.
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
##############################################################################

## @package optimizer
# Module caffe2.python.optimizer
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

from collections import namedtuple, defaultdict
from past.builtins import basestring

import numpy as np

from caffe2.python import core, scope, workspace
from caffe2.python.modeling import parameter_info
from caffe2.proto import caffe2_pb2


_OPTIMIZER_ITERATION_NAME = "optimizer_iteration"
_LEARNING_RATE_INJECTION = "lr_injection"

AuxOptimizerParams = namedtuple("AuxOptimizerParams", ["local", "shared"])
_optimizer_instance_count = defaultdict(int)


class Optimizer(object):
    def __init__(self):
        self._aux_params = AuxOptimizerParams(local=[], shared=[])
        self._instance_num = _optimizer_instance_count[self.__class__.__name__]
        _optimizer_instance_count[self.__class__.__name__] += 1
        self._lr_multiplier = None

    '''
    Adds optimization operators to the net for given parameter and its gradient
    Parameter is specified by either 'param' being a ParameterInfo object.
    In this case  param.grad has to be set

    Or by 'param' being a BlobReference and 'grad' being a BlobReference for its
    gradient.
    '''
    def __call__(self, net, param_init_net, param, grad=None):
        if grad is None:
            assert isinstance(param, parameter_info.ParameterInfo)
            assert param.grad is not None
        else:
            if isinstance(param, basestring):
                param = core.BlobReference(param)
            param = parameter_info.ParameterInfo(
                param_id=None, param=param, grad=grad)

        self._run(net, param_init_net, param)

    def _run(self, net, param_init_net, param_info):
        raise Exception("Not Implemented")

    def get_cpu_blob_name(self, base_str, node_name=''):
        classname = self.__class__.__name__
        return '%s_%d_%s%s_cpu' % (classname, self._instance_num, base_str, node_name)

    def get_gpu_blob_name(self, base_str, gpu_id, node_name):
        classname = self.__class__.__name__
        return '%s_%d_%s%s_gpu%d' % (
            classname, self._instance_num, base_str, node_name, gpu_id,
        )

    def make_unique_blob_name(self, base_str):
        """
        Returns a blob name that will be unique to the current device
        and optimizer instance.
        """
        current_scope = scope.CurrentDeviceScope()
        if current_scope is None:
            return self.get_cpu_blob_name(base_str)

        if current_scope.device_type == caffe2_pb2.CUDA:
            return self.get_gpu_blob_name(
                base_str, current_scope.cuda_gpu_id, current_scope.node_name
            )
        else:
            return self.get_cpu_blob_name(base_str, current_scope.node_name)

    def build_lr(self, net, param_init_net, base_learning_rate,
                 learning_rate_blob=None, policy="fixed",
                 iter_val=0, **kwargs):
        if learning_rate_blob is None:
            learning_rate_blob = self.make_unique_blob_name('lr')

        # Each node needs its own iteration counter
        current_scope = scope.CurrentDeviceScope()
        node_name = current_scope.node_name if current_scope else ''
        optimization_iter_blob = _OPTIMIZER_ITERATION_NAME + node_name
        if not param_init_net.BlobIsDefined(optimization_iter_blob):
            # Add training operators.
            with core.DeviceScope(core.DeviceOption(caffe2_pb2.CPU)):
                iteration = param_init_net.ConstantFill(
                    [], optimization_iter_blob, shape=[1],
                    value=iter_val,
                    dtype=core.DataType.INT64)
                iter_mutex = param_init_net.CreateMutex(
                    [], ["iteration_mutex" + node_name]
                )
                net.AtomicIter([iter_mutex, iteration], [iteration])
        else:
            iteration = param_init_net.GetBlobRef(optimization_iter_blob)

        if not net.BlobIsDefined(learning_rate_blob):
            # There is one interesting thing here: since we are minimizing, we are
            # doing "descent" so the learning rate is set to be negative.
            lr = net.LearningRate(
                [iteration],
                learning_rate_blob,
                base_lr=-base_learning_rate,
                policy=policy,
                **kwargs
            )
        else:
            lr = net.GetBlobRef(learning_rate_blob)

        if self._lr_multiplier is not None:
            lr_multiplier = net.CopyFromCPUInput(
                self._lr_multiplier, self.make_unique_blob_name('lr_multiplier')
            )
            scaled_lr = net.Mul(
                [lr, lr_multiplier],
                self.make_unique_blob_name('scaled_lr'),
                broadcast=1,
            )
            lr = scaled_lr

        return lr, iteration

    def add_lr_multiplier(self, lr_multiplier):
        self._lr_multiplier = lr_multiplier

    @staticmethod
    def dedup(net, sparse_dedup_aggregator, grad):
        assert (isinstance(grad, core.GradientSlice))
        if sparse_dedup_aggregator:
            return net.DeduplicateGradientSlices(
                grad, aggregator=sparse_dedup_aggregator)
        else:
            return grad

    def get_auxiliary_parameters(self):
        """Returns a list of auxiliary parameters.

        Returns:
            aux_params: A namedtuple, AuxParams.

            aux_params.local stores a list of blobs. Each blob is a local
            auxiliary parameter. A local auxiliary parameter is a parameter in
            parallel to a learning rate parameter. Take adagrad as an example,
            the local auxiliary parameter is the squared sum parameter, because
            every learning rate has a squared sum associated with it.

            aux_params.shared also stores a list of blobs. Each blob is a shared
            auxiliary parameter. A shared auxiliary parameter is a parameter
            that is shared across all the learning rate parameters. Take adam as
            an example, the iteration parameter is a shared parameter, because
            all the learning rates share the same iteration parameter.
        """
        return self._aux_params

    # TODO(xlwang): In transfer learning, parameter initialized from pretrained
    # model might require a different learning rate than otherwise initialized.
    # To this end, here we implement a python solution where
    # `base_learning_rate` is scaled by `scale`, by calling
    # `scale_learning_rate`; Alternatively, we can achieve same effect by
    # rewriting the LearningRate operator in C++
    # Note that it is the responsibility of specific optimizer to decide what
    # logic should be used for `scale_learning_rate`
    def scale_learning_rate(self, *args, **kwargs):
        raise NotImplementedError(
            "Optimizer Need to Implement `scale_learning_rate` method.")


class SgdOptimizer(Optimizer):
    def __init__(self, base_learning_rate=0.01, policy='fixed',
                 momentum=0.0, nesterov=1, sparse_dedup_aggregator=None,
                 **kwargs):
        super(SgdOptimizer, self).__init__()
        self.base_learning_rate = base_learning_rate
        self.policy = policy
        self.momentum = momentum
        self.nesterov = nesterov
        self.sparse_dedup_aggregator = sparse_dedup_aggregator
        self.init_kwargs = kwargs

    def _run(self, net, param_init_net, param_info):
        param = param_info.blob
        grad = param_info.grad
        if self.base_learning_rate == 0:
            return
        assert self.base_learning_rate > 0

        # We need negative sign for LR when used directly with WeightedSum
        # below.
        lr_sign = -1 if self.momentum else 1
        lr, _ = self.build_lr(
            net, param_init_net,
            base_learning_rate=self.base_learning_rate * lr_sign,
            policy=self.policy,
            **(self.init_kwargs)
        )

        dev = scope.CurrentDeviceScope()
        if dev is None:
            dev = core.DeviceOption(caffe2_pb2.CPU)

        # Each GPU/CPU must have its own ONE blob, thus modify the name
        # to include device information.
        ONE = param_init_net.ConstantFill(
            [],
            "ONE_{}_{}{}".format(dev.device_type, dev.cuda_gpu_id, dev.node_name),
            shape=[1],
            value=1.0
        )

        self._aux_params.shared.append(ONE)

        if self.momentum > 0:
            momentum_data = param_init_net.ConstantFill(
                param, str(param) + "_momentum", value=0.)
            self._aux_params.local.append(momentum_data)

        if isinstance(grad, core.GradientSlice):
            grad = self.dedup(net, self.sparse_dedup_aggregator, grad)
            if self.momentum > 0.:
                net.SparseMomentumSGDUpdate(
                    [grad.values, momentum_data, lr, param, grad.indices],
                    [grad.values, momentum_data, param],
                    momentum=self.momentum,
                    nesterov=self.nesterov)
            else:
                net.ScatterWeightedSum(
                    [param, ONE, grad.indices, grad.values, lr],
                    param
                )
        else:
            if self.momentum > 0.:
                net.MomentumSGDUpdate(
                    [grad, momentum_data, lr, param],
                    [grad, momentum_data, param],
                    momentum=self.momentum,
                    nesterov=self.nesterov)
            else:
                coeff = lr

                net.WeightedSum(
                    [param, ONE, grad, coeff],
                    param
                )

    def scale_learning_rate(self, scale):
        self.base_learning_rate *= scale
        return


class MultiPrecisionSgdOptimizer(SgdOptimizer):
    def __init__(self, base_learning_rate=0.1, momentum=0.0,
                 policy="fixed", nesterov=1, sparse_dedup_aggregator=None,
                 **kwargs):
        super(SgdOptimizer, self).__init__()
        self.base_learning_rate = base_learning_rate
        self.momentum = momentum
        self.policy = policy
        self.nesterov = nesterov
        self.sparse_dedup_aggregator = sparse_dedup_aggregator
        self.init_kwargs = kwargs

    def _run(self, net, param_init_net, param_info):
        param = param_info.blob
        param_fp32 = param_info.blob_copy[core.DataType.FLOAT] \
                if param_info.blob_copy is not None else None

        # If we have a straight fp32 parameter, run the base class
        if param_fp32 is None:
            return SgdOptimizer._run(self, net, param_init_net, param_info)

        grad = param_info.grad
        if self.base_learning_rate == 0:
            return
        assert self.base_learning_rate > 0

        lr, _ = self.build_lr(
            net, param_init_net,
            base_learning_rate=-self.base_learning_rate,
            policy=self.policy,
            **(self.init_kwargs)
        )

        momentum_data = param_init_net.ConstantFill(
            param_fp32, str(param) + "_momentum", value=0.)
        self._aux_params.local.append(momentum_data)

        assert not isinstance(grad, core.GradientSlice), \
                "Doesn't support sparse gradients"

        # Copy gradient to fp32
        grad_fp32 = net.HalfToFloat(grad, grad + "_fp32")

        # update (fused) in fp32
        net.MomentumSGDUpdate(
            [grad_fp32, momentum_data, lr, param_fp32],
            [grad_fp32, momentum_data, param_fp32],
            momentum=self.momentum,
            nesterov=self.nesterov)

        # Copy updated param back to fp16
        net.FloatToHalf(param_fp32, param)


class FP16SgdOptimizer(SgdOptimizer):
    def __init__(self, base_learning_rate=0.1, momentum=0.0,
                 policy="fixed", nesterov=1, weight_decay=0.0001,
                 sparse_dedup_aggregator=None,
                 **kwargs):
        super(SgdOptimizer, self).__init__()
        self.base_learning_rate = base_learning_rate
        self.momentum = momentum
        self.policy = policy
        self.nesterov = nesterov
        self.sparse_dedup_aggregator = sparse_dedup_aggregator
        self.init_kwargs = kwargs
        self.weight_decay = weight_decay

    def _run(self, net, param_init_net, param_info, fp32_update=False):

        fp32_update_flag = 0
        param_name = str(param_info.blob)

        # should only be triggered in FP16 training by SpatialBN, which
        # requires FP32 params in CuDNN.
        if param_name.find("spatbn") != -1:
            fp32_update = True

        if fp32_update:
            # doing a 32bit update
            # Have to assume param_info.blob is FP32 as there is no way
            # (that i currently know of) to query a blob's type in python
            fp32_update_flag = 1
            param = param_info.blob
            param_fp32 = param_info.blob
        else:
            if param_info.blob_copy is None:
                # doing a 32bit update
                # Have to assume param_info.blob is FP32 as there is no way
                # (that i currently know of) to query a blob's type in python
                fp32_update_flag = 1
                param = param_info.blob
                param_fp32 = param_info.blob
            else:
                if core.DataType.FLOAT in param_info.blob_copy:
                    param = param_info.blob
                    param_fp32 = param_info.blob_copy[core.DataType.FLOAT]
                elif core.DataType.FLOAT16 in param_info.blob_copy:
                    param = param_info.blob_copy[core.DataType.FLOAT16]
                    param_fp32 = param_info.blob
                else:
                    assert (False), (
                        "Unrecognized parameter format to be updated "
                        "by FP16 Optimizer. Parameter: {}".format(param_info.name)
                    )

        grad = param_info.grad

        if self.base_learning_rate == 0:
            return
        assert self.base_learning_rate > 0

        lr, _ = self.build_lr(
            net, param_init_net,
            base_learning_rate=-self.base_learning_rate,
            policy=self.policy,
            **(self.init_kwargs)
        )

        momentum_data_fp32 = param_init_net.ConstantFill(
            param_fp32, str(param) + "_momentum_fp32", value=0.)

        momentum_data = param_init_net.FloatToHalf(
            momentum_data_fp32, str(param) + "_momentum")

        self._aux_params.local.append(momentum_data)

        assert not isinstance(grad, core.GradientSlice), \
                "Doesn't support sparse gradients"

        if fp32_update_flag == 0:
            net.FP16MomentumSGDUpdate(
                [grad, momentum_data, lr, param],
                [grad, momentum_data, param],
                momentum=self.momentum,
                nesterov=self.nesterov,
                weight_decay=self.weight_decay)
        else:
            # flag set to 1, therefore doing FP32 update
            net.FP32MomentumSGDUpdate(
                [grad, momentum_data_fp32, lr, param],
                [grad, momentum_data_fp32, param],
                momentum=self.momentum,
                nesterov=self.nesterov,
                weight_decay=self.weight_decay)


class WeightDecayBuilder(Optimizer):
    def __init__(self, weight_decay):
        self.weight_decay = weight_decay

    def _run(self, net, param_init_net, param_info):
        dev = scope.CurrentDeviceScope()
        if dev is None:
            dev = core.DeviceOption(caffe2_pb2.CPU)

        ONE = param_init_net.ConstantFill(
            [],
            "ONE_{}_{}".format(dev.device_type, dev.cuda_gpu_id),
            shape=[1],
            value=1.0
        )
        WD = param_init_net.ConstantFill(
            [], "wd_{}_{}".format(dev.device_type, dev.cuda_gpu_id),
            shape=[1], value=self.weight_decay
        )

        if isinstance(param_info.grad, core.GradientSlice):
            assert "Weight decay does not yet support sparse gradients"
        else:
            net.WeightedSum(
                [param_info.grad, ONE, param_info.blob, WD],
                param_info.grad,
            )


class AdagradOptimizer(Optimizer):
    def __init__(self, alpha=0.01, epsilon=1e-4, decay=1, policy="fixed",
                 sparse_dedup_aggregator=None, rowWise=False,
                 engine='', **kwargs):
        super(AdagradOptimizer, self).__init__()
        self.alpha = alpha
        self.epsilon = epsilon
        self.decay = decay
        self.policy = policy
        self.sparse_dedup_aggregator = sparse_dedup_aggregator
        self.engine = engine
        self.init_kwargs = kwargs
        self.rowWise = rowWise

    def _run(self, net, param_init_net, param_info):
        param = param_info.blob
        grad = param_info.grad

        if self.alpha <= 0:
            return

        lr, _ = self.build_lr(
            net, param_init_net,
            base_learning_rate=self.alpha,
            policy=self.policy,
            **(self.init_kwargs)
        )

        if self.rowWise:
            shape = param_init_net.Shape(param, str(param) + "_shape")
            num_rows = param_init_net.Slice(
                [shape],
                str(shape) + "_numrows",
                starts=[0], ends=[1]
            )
            param_squared_sum = param_init_net.ConstantFill(
                num_rows,
                str(param) + "_avg_squared_sum",
                input_as_shape=1,
                value=0.0
            )
        else:
            param_squared_sum = param_init_net.ConstantFill(
                [param],
                str(param) + "_squared_sum",
                value=0.0
            )

        self._aux_params.local.append(param_squared_sum)

        if self.rowWise:
            assert isinstance(grad, core.GradientSlice),\
                'If SparseAdagrad with rowWise=True, gradient must be '\
                'a gradientslice. PLease ensure that rowWise is not enabled '\
                'for the dense Adagrad optimizer, as it is not supported.'
        if isinstance(grad, core.GradientSlice):
            assert self.decay == 1.,\
                'Decay is not implemented for SparseAdagrad and must be set to 1'
            grad = self.dedup(net, self.sparse_dedup_aggregator, grad)
            if self.rowWise:
                op = 'RowWiseSparseAdagrad'
            else:
                op = 'SparseAdagrad'
            net.__getattr__(op)(
                [param, param_squared_sum, grad.indices, grad.values, lr],
                [param, param_squared_sum],
                epsilon=self.epsilon,
                engine=self.engine
            )
        else:
            net.Adagrad(
                [param, param_squared_sum, grad, lr],
                [param, param_squared_sum],
                epsilon=self.epsilon,
                decay=float(self.decay),
                engine=self.engine
            )

    def scale_learning_rate(self, scale):
        self.alpha *= scale
        return


class FtrlOptimizer(Optimizer):
    def __init__(self, alpha=0.01, beta=1e-4, lambda1=0, lambda2=0,
                 sparse_dedup_aggregator=None, engine=''):
        super(FtrlOptimizer, self).__init__()
        self.alpha = alpha
        self.beta = beta
        self.lambda1 = lambda1
        self.lambda2 = lambda2
        self.sparse_dedup_aggregator = sparse_dedup_aggregator
        self.engine = engine

    def _run(self, net, param_init_net, param_info):
        param = param_info.blob
        grad = param_info.grad

        if self.alpha <= 0:
            return

        nz = param_init_net.ConstantFill(
            [param],
            str(param) + "_ftrl_nz",
            extra_shape=[2],
            value=0.0
        )
        self._aux_params.local.append(nz)
        if isinstance(grad, core.GradientSlice):
            grad = self.dedup(net, self.sparse_dedup_aggregator, grad)
            net.SparseFtrl(
                [param, nz, grad.indices, grad.values],
                [param, nz],
                engine=self.engine,
                alpha=self.alpha,
                beta=self.beta,
                lambda1=self.lambda1,
                lambda2=self.lambda2
            )
        else:
            net.Ftrl(
                [param, nz, grad],
                [param, nz],
                engine=self.engine,
                alpha=self.alpha,
                beta=self.beta,
                lambda1=self.lambda1,
                lambda2=self.lambda2
            )

    def scale_learning_rate(self, scale):
        self.alpha *= scale
        return


class AdamOptimizer(Optimizer):
    def __init__(self, alpha=0.001, beta1=0.9, beta2=0.999, epsilon=1e-8,
                 policy='fixed', sparse_dedup_aggregator=None,
                 engine='', **kwargs):
        super(AdamOptimizer, self).__init__()
        self.alpha = alpha
        self.beta1 = beta1
        self.beta2 = beta2
        self.epsilon = epsilon
        self.policy = policy
        self.sparse_dedup_aggregator = sparse_dedup_aggregator
        self.engine = engine
        self.init_kwargs = kwargs

    def _run(self, net, param_init_net, param_info):
        param = param_info.blob
        grad = param_info.grad

        if self.alpha <= 0:
            return

        lr, iteration = self.build_lr(
            net, param_init_net,
            base_learning_rate=self.alpha,
            policy=self.policy,
            **(self.init_kwargs)
        )

        m1 = param_init_net.ConstantFill(
            [param],
            param + "_first_moment",
            value=0.0
        )
        m2 = param_init_net.ConstantFill(
            [param],
            param + "_second_moment",
            value=0.0
        )
        self._aux_params.shared.append(iteration)
        self._aux_params.local.append(m1)
        self._aux_params.local.append(m2)
        if isinstance(grad, core.GradientSlice):
            grad = self.dedup(net, self.sparse_dedup_aggregator, grad)
            net.SparseAdam(
                [param, m1, m2, grad.indices, grad.values, lr, iteration],
                [param, m1, m2],
                beta1=self.beta1,
                beta2=self.beta2,
                epsilon=self.epsilon
            )

        else:
            net.Adam(
                [param, m1, m2, grad, lr, iteration],
                [param, m1, m2],
                beta1=self.beta1,
                beta2=self.beta2,
                epsilon=self.epsilon)

    def scale_learning_rate(self, scale):
        self.alpha *= scale
        return


class YellowFinOptimizer(Optimizer):
    """YellowFin: An automatic tuner for momentum SGD

    See https://arxiv.org/abs/1706.03471 for more details. This implementation
    has separate learning rate and momentum per each parameter."""

    def __init__(self,
                 alpha=0.1,
                 mu=0.0,
                 beta=0.999,
                 curv_win_width=20,
                 zero_debias=True,
                 epsilon=0.1**6,
                 policy='fixed',
                 sparse_dedup_aggregator=None,
                 **kwargs):
        super(YellowFinOptimizer, self).__init__()
        self.alpha = alpha
        self.mu = mu
        self.beta = beta
        self.curv_win_width = curv_win_width
        self.zero_debias = zero_debias
        self.epsilon = epsilon
        self.policy = policy
        self.sparse_dedup_aggregator = sparse_dedup_aggregator
        self.init_kwargs = kwargs

    def _run(self, net, param_init_net, param_info):

        # Note: This is number of persistent scalars in YellowFin optimizer.
        #       It should always be the number of scalars being used. The same
        #       number should be used in class for the operation.
        SCALARS_MEMORY_SIZE = 5

        param = param_info.blob
        grad = param_info.grad
        moment = param_init_net.ConstantFill(
            [param],
            param + "_moment",
            value=0.0
        )
        curv_win = param_init_net.ConstantFill(
            [],
            param + "_curv_win",
            shape=[self.curv_win_width],
            value=0.0
        )
        g_avg = param_init_net.ConstantFill(
            [param],
            param + "_g_avg",
            value=0.0
        )
        g2_avg = param_init_net.ConstantFill(
            [param],
            param + "_g2_avg",
            value=0.0
        )
        lr_avg = param_init_net.ConstantFill(
            [],
            param + "_lr_avg",
            shape=[1],
            value=self.alpha
        )
        mu_avg = param_init_net.ConstantFill(
            [],
            param + "_mu_avg",
            shape=[1],
            value=self.mu
        )
        scalars_memory = param_init_net.ConstantFill(
            [],
            param + "_scalars_memory",
            shape=[SCALARS_MEMORY_SIZE],
            value=0.0
        )

        assert self.alpha > 0
        assert not isinstance(grad, core.GradientSlice), \
            "Doesn't support sparse gradients"

        if not param_init_net.BlobIsDefined(_OPTIMIZER_ITERATION_NAME):
            # Add training operators.
            with core.DeviceScope(core.DeviceOption(caffe2_pb2.CPU)):
                iteration = param_init_net.ConstantFill(
                    [],
                    _OPTIMIZER_ITERATION_NAME,
                    shape=[1],
                    value=0,
                    dtype=core.DataType.INT64)
                iter_mutex = param_init_net.CreateMutex([],
                                                        ["iteration_mutex"])
                net.AtomicIter([iter_mutex, iteration], [iteration])
        else:
            iteration = param_init_net.GetBlobRef(_OPTIMIZER_ITERATION_NAME)

        self._aux_params.shared.append(iteration)
        self._aux_params.local.append(moment)
        self._aux_params.local.append(lr_avg)
        self._aux_params.local.append(mu_avg)
        self._aux_params.local.append(curv_win)
        self._aux_params.local.append(g_avg)
        self._aux_params.local.append(g2_avg)
        self._aux_params.local.append(scalars_memory)

        yf_in_out_args = [
            param,
            moment,
            lr_avg,
            mu_avg,
            curv_win,
            g_avg,
            g2_avg,
            scalars_memory
        ]

        net.YellowFin(
            yf_in_out_args + [grad, iteration],
            yf_in_out_args,
            beta=self.beta,
            epsilon=self.epsilon,
            curv_win_width=self.curv_win_width,
            zero_debias=self.zero_debias)

    def scale_learning_rate(self, scale):
        self.alpha *= scale
        return


def _get_param_to_device(model):
    # Infer blob devices by going through the net and param_init_net
    # ops and observing the device used to create or use the blob.
    param_to_device = core.InferBlobDevices(model.net)
    param_to_device.update(core.InferBlobDevices(model.param_init_net))
    return param_to_device


def get_param_device(param_name, grad, param_to_device=None, default_device=None):
    device = default_device
    param_to_device = param_to_device or {}
    # We first check if parameter's device has been inferred. If not,
    # we check the gradient. This can happen if parameter is not output
    # by any blob but created by a FetchBlob.
    if param_name in param_to_device:
        device = param_to_device[param_name]
    else:
        if isinstance(grad, core.GradientSlice):
            grad = grad
            if str(grad.values) in param_to_device:
                device = param_to_device[str(grad.values)]
            elif str(grad.indices) in param_to_device:
                device = param_to_device[str(grad.indices)]
        else:
            grad_name = str(grad)
            if grad_name in param_to_device:
                device = param_to_device[grad_name]

    assert device is not None,\
        "Cannot infer device for {}: no op creates it".format(param_name)
    return device


def get_lr_injection():
    """
    Gets current value for lr_injection, a multiplier for all base
    learning rates.
    Must set allow_lr_injection=True when building optimizer, as it
    relies on synchronization over CPU.
    """
    return workspace.FetchBlob(_LEARNING_RATE_INJECTION)


def set_lr_injection(lr_injection_value):
    """
    Sets lr_injection, a multiplier for all base learning rates.
    Must set allow_lr_injection=True when building optimizer, as it
    relies on synchronization over CPU.
    """
    workspace.FeedBlob(
        _LEARNING_RATE_INJECTION,
        np.array(
            [float(lr_injection_value)],
            dtype=np.float32,
        ),
    )


def _calc_norm_ratio(
    model, params, name_scope, param_to_device, max_gradient_norm
):
    with core.NameScope(name_scope):
        grad_squared_sums = []
        for i, param in enumerate(params):
            device = get_param_device(
                str(param.blob), param.grad, param_to_device
            )

            with core.DeviceScope(device):
                grad = (
                    param.grad
                    if not isinstance(
                        param.grad,
                        core.GradientSlice,
                    ) else param.grad.values
                )

                grad_squared_sum_name = 'grad_{}_squared_sum'.format(i)
                grad_squared_sum = model.net.SumSqrElements(
                    grad,
                    grad_squared_sum_name,
                )
                grad_squared_sum_cpu = model.net.EnsureCPUOutput(
                    grad_squared_sum
                )
                grad_squared_sums.append(grad_squared_sum_cpu)

        with core.DeviceScope(core.DeviceOption(caffe2_pb2.CPU)):
            grad_squared_full_sum = model.net.Sum(
                grad_squared_sums,
                'grad_squared_full_sum',
            )
            global_norm = model.net.Pow(
                grad_squared_full_sum,
                'global_norm',
                exponent=0.5,
            )
            clip_norm = model.param_init_net.ConstantFill(
                [],
                'clip_norm',
                shape=[],
                value=float(max_gradient_norm),
            )
            max_norm = model.net.Max(
                [global_norm, clip_norm],
                'max_norm',
            )
            norm_ratio = model.net.Div(
                [clip_norm, max_norm],
                'norm_ratio',
            )
            return norm_ratio


def _build(
    model,
    optimizer,
    weights_only=False,
    use_param_info_optim=True,
    max_gradient_norm=None,
    allow_lr_injection=False,
):
    param_to_device = _get_param_to_device(model)

    # Validate there are no duplicate params
    model.Validate()

    params = []
    for param_info in model.GetOptimizationParamInfo():
        if weights_only and param_info.blob not in model.weights:
            continue
        params.append(param_info)

    lr_multiplier = None
    if max_gradient_norm is not None:
        lr_multiplier = _calc_norm_ratio(
            model,
            params,
            'norm_clipped_grad_update',
            param_to_device,
            max_gradient_norm,
        )

    if allow_lr_injection:
        if not model.net.BlobIsDefined(_LEARNING_RATE_INJECTION):
            lr_injection = model.param_init_net.ConstantFill(
                [],
                _LEARNING_RATE_INJECTION,
                shape=[1],
                value=1.0,
            )
        else:
            lr_injection = _LEARNING_RATE_INJECTION

        if lr_multiplier is None:
            lr_multiplier = lr_injection
        else:
            lr_multiplier = model.net.Mul(
                [lr_multiplier, lr_injection],
                'lr_multiplier',
                broadcast=1,
            )
    optimizer.add_lr_multiplier(lr_multiplier)

    for param_info in params:
        param_name = str(param_info.blob)

        device = get_param_device(param_name, param_info.grad, param_to_device)

        with core.DeviceScope(device):
            if param_info.optimizer and use_param_info_optim:
                param_info.optimizer(model.net, model.param_init_net, param_info)
            else:
                optimizer(model.net, model.param_init_net, param_info)
    return optimizer


def add_weight_decay(model, weight_decay):
    """Adds a decay to weights in the model.

    This is a form of L2 regularization.

    Args:
        weight_decay: strength of the regularization
    """
    _build(
        model,
        WeightDecayBuilder(weight_decay=weight_decay),
        weights_only=True,
        use_param_info_optim=False,
    )


def build_sgd(
    model,
    base_learning_rate,
    max_gradient_norm=None,
    allow_lr_injection=False,
    **kwargs
):
    sgd_optimizer = SgdOptimizer(base_learning_rate, **kwargs)
    return _build(
        model,
        sgd_optimizer,
        max_gradient_norm=max_gradient_norm,
        allow_lr_injection=allow_lr_injection,
    )


def build_multi_precision_sgd(
    model,
    base_learning_rate,
    max_gradient_norm=None,
    allow_lr_injection=False,
    **kwargs
):
    multi_prec_sgd_optimizer = MultiPrecisionSgdOptimizer(
        base_learning_rate, **kwargs
    )
    return _build(
        model,
        multi_prec_sgd_optimizer,
        max_gradient_norm=max_gradient_norm,
        allow_lr_injection=allow_lr_injection,
    )


def build_fp16_sgd(model, base_learning_rate, **kwargs):
    fp16_sgd_optimizer = FP16SgdOptimizer(
        base_learning_rate, **kwargs
    )
    return _build(model, fp16_sgd_optimizer)


def build_ftrl(model, engine="SIMD", **kwargs):
    if engine == "SIMD":
        assert core.IsOperator('Ftrl_ENGINE_SIMD')
        assert core.IsOperator('SparseFtrl_ENGINE_SIMD')
    ftrl_optimizer = FtrlOptimizer(engine=engine, **kwargs)
    return _build(model, ftrl_optimizer)


def build_adagrad(
    model,
    base_learning_rate,
    parameters=None,
    max_gradient_norm=None,
    allow_lr_injection=False,
    **kwargs
):
    adagrad_optimizer = AdagradOptimizer(alpha=base_learning_rate, **kwargs)
    return _build(
        model,
        adagrad_optimizer,
        max_gradient_norm=max_gradient_norm,
        allow_lr_injection=allow_lr_injection,
    )


def build_adam(
    model,
    base_learning_rate,
    max_gradient_norm=None,
    allow_lr_injection=False,
    **kwargs
):
    adam_optimizer = AdamOptimizer(alpha=base_learning_rate, **kwargs)
    return _build(
        model,
        adam_optimizer,
        max_gradient_norm=max_gradient_norm,
        allow_lr_injection=allow_lr_injection,
    )


def build_yellowfin(model, base_learning_rate=0.1, **kwargs):
    yellowfin_optimizer = YellowFinOptimizer(
        alpha=base_learning_rate,
        **kwargs)
    return _build(model, yellowfin_optimizer)
