"""
Optimizations addressing the ops in nnet root directory
"""

import theano
from theano import compile, gof
from theano.gof import local_optimizer

from theano.tensor.nnet.corr import (
    CorrMM, CorrMM_gradInputs, CorrMM_gradWeights)
from theano.tensor.nnet.blocksparse import (
    SparseBlockGemv,
    SparseBlockOuter,
    sparse_block_gemv_inplace,
    sparse_block_outer_inplace)
from theano.tensor.nnet.abstract_conv import (AbstractConv2d,
                                              AbstractConv2d_gradWeights,
                                              AbstractConv2d_gradInputs)
from theano.tensor.nnet.abstract_conv import get_conv_output_shape
from theano.tensor.opt import (copy_stack_trace,
                               register_specialize_device)
from theano.tensor import TensorType

# Cpu implementation
from theano.tensor.nnet.conv import conv2d, ConvOp
from theano.tensor.nnet.ConvGrad3D import convGrad3D
from theano.tensor.nnet.ConvTransp3D import convTransp3D


@gof.local_optimizer([SparseBlockGemv], inplace=True)
def local_inplace_sparse_block_gemv(node):
    """
        SparseBlockGemv(inplace=False) -> SparseBlockGemv(inplace=True)
    """
    if isinstance(node.op, SparseBlockGemv) and not node.op.inplace:
        new_node = sparse_block_gemv_inplace(*node.inputs)
        return [new_node]
    return False
compile.optdb.register('local_inplace_sparse_block_gemv',
                       gof.TopoOptimizer(
                           local_inplace_sparse_block_gemv,
                           failure_callback=gof.TopoOptimizer.warn_inplace),
                       60, 'fast_run', 'inplace')  # DEBUG


@gof.local_optimizer([SparseBlockOuter], inplace=True)
def local_inplace_sparse_block_outer(node):
    """
        SparseBlockOuter(inplace=False) -> SparseBlockOuter(inplace=True)
    """
    if isinstance(node.op, SparseBlockOuter) and not node.op.inplace:
        new_node = sparse_block_outer_inplace(*node.inputs)
        return [new_node]
    return False
compile.optdb.register('local_inplace_sparse_block_outer',
                       gof.TopoOptimizer(
                           local_inplace_sparse_block_outer,
                           failure_callback=gof.TopoOptimizer.warn_inplace),
                       60, 'fast_run', 'inplace')  # DEBUG


# Conv opts
@local_optimizer([AbstractConv2d])
def local_abstractconv_gemm(node):
    if theano.config.cxx == "":
        return
    if not isinstance(node.op, AbstractConv2d):
        return None
    img, kern = node.inputs
    if not isinstance(img.type, TensorType) or \
       not isinstance(kern.type, TensorType):
        return None

    # need to flip the kernel if necessary
    if node.op.filter_flip:
        kern = kern[:, :, ::-1, ::-1]
    rval = CorrMM(border_mode=node.op.border_mode,
                  subsample=node.op.subsample)(img, kern)
    copy_stack_trace(node.outputs[0], rval)

    return [rval]


@local_optimizer([AbstractConv2d_gradWeights])
def local_abstractconv_gradweight_gemm(node):
    if theano.config.cxx == "":
        return
    if not isinstance(node.op, AbstractConv2d_gradWeights):
        return None
    img, topgrad, shape = node.inputs
    if not isinstance(img.type, TensorType) or \
       not isinstance(topgrad.type, TensorType):
        return None

    rval = CorrMM_gradWeights(border_mode=node.op.border_mode,
                              subsample=node.op.subsample)(img, topgrad, shape)
    copy_stack_trace(node.outputs[0], rval)

    # need to flip the kernel if necessary
    if node.op.filter_flip:
        rval = rval[:, :, ::-1, ::-1]
    rval = theano.tensor.patternbroadcast(rval, node.outputs[0].broadcastable)
    copy_stack_trace(node.outputs[0], rval)

    return [rval]


@local_optimizer([AbstractConv2d_gradInputs])
def local_abstractconv_gradinputs_gemm(node):
    if theano.config.cxx == "":
        return
    if not isinstance(node.op, AbstractConv2d_gradInputs):
        return None
    kern, topgrad, shape = node.inputs
    if not isinstance(kern.type, TensorType) or \
       not isinstance(topgrad.type, TensorType):
        return None

    # need to flip the kernel if necessary
    if node.op.filter_flip:
        kern = kern[:, :, ::-1, ::-1]
    rval = CorrMM_gradInputs(border_mode=node.op.border_mode,
                             subsample=node.op.subsample)(kern, topgrad,
                                                          shape)
    copy_stack_trace(node.outputs[0], rval)

    return [rval]


@local_optimizer([AbstractConv2d])
def local_conv2d_cpu(node):

    if not isinstance(node.op, AbstractConv2d):
        return None

    img, kern = node.inputs
    if ((not isinstance(img.type, TensorType) or
         not isinstance(kern.type, TensorType))):
        return None
    if node.op.border_mode not in ['full', 'valid']:
        return None
    if not node.op.filter_flip:
        # Not tested yet
        return None

    rval = conv2d(img, kern,
                  node.op.imshp, node.op.kshp,
                  border_mode=node.op.border_mode,
                  subsample=node.op.subsample)

    copy_stack_trace(node.outputs[0], rval)
    return [rval]


@local_optimizer([AbstractConv2d_gradWeights])
def local_conv2d_gradweight_cpu(node):
    if not isinstance(node.op, AbstractConv2d_gradWeights):
        return None

    img, topgrad, shape = node.inputs

    if ((not isinstance(img.type, TensorType) or
         not isinstance(topgrad.type, TensorType))):
        return None
    if node.op.border_mode not in ['full', 'valid']:
        return None
    if not node.op.filter_flip:
        # Not tested yet
        return

    if node.op.border_mode == 'valid' and \
            (node.op.subsample != (1, 1)):
        # Use the gradient as defined in conv3D, because the implementation
        # by Conv is slow (about 3x slower than conv3D, and probably 10x
        # slower than it could be), and incorrect when subsample > 2.
        # build a "node", that should be equivalent to the one given by
        # self.make_node, but using convGrad3D instead.
        shuffled_img = img.dimshuffle(0, 2, 3, 'x', 1)
        shuffled_topgrad = topgrad.dimshuffle(0, 2, 3, 'x', 1)
        rval = convGrad3D(V=shuffled_img,
                          d=(node.op.subsample[0], node.op.subsample[1], 1),
                          WShape=(shuffled_topgrad.shape[4],
                                  shape[0], shape[1], 1,
                                  shuffled_img.shape[4]),
                          dCdH=shuffled_topgrad)
        copy_stack_trace(node.outputs[0], rval)

        rval = theano.tensor.addbroadcast(rval, 3)
        rval = rval.dimshuffle(0, 4, 1, 2)
        rval = rval[:, :, ::-1, ::-1]
        rval = theano.tensor.patternbroadcast(rval,
                                              node.outputs[0].broadcastable)
        copy_stack_trace(node.outputs[0], rval)
        return [rval]

    dx, dy = node.op.subsample
    if dx not in (1, 2) or dy not in (1, 2):
        # Not implemented in the gradient of ConvOp
        return None

    if node.op.imshp is None:
        op_imshp = (None, None, None, None)
    else:
        op_imshp = node.op.imshp

    if node.op.kshp is None:
        op_kshp = (None, None, None, None)
    else:
        op_kshp = node.op.kshp

    if None in op_imshp or None in op_kshp:
        if (dx, dy) != (1, 1):
            # We cannot infer the shapes
            return None

    # Determine gradient on kernels
    assert len(op_imshp) == 4 and len(op_kshp) == 4

    outshp = get_conv_output_shape(op_imshp, op_kshp,
                                   node.op.border_mode, node.op.subsample)[2:]
    fulloutshp = get_conv_output_shape(op_imshp, op_kshp,
                                       node.op.border_mode, (1, 1))[2:]

    newimg = img.dimshuffle((1, 0, 2, 3))
    newtopgrad = topgrad.dimshuffle((1, 0, 2, 3))

    if node.op.border_mode == 'valid':
        (img, filters) = (newimg, newtopgrad)
        kshp_logical = fulloutshp
        kshp_logical_top_aligned = False
        imshp_logical = None
        (bsize, nkern) = (op_imshp[1], op_kshp[0])
        imshp = (op_imshp[0], op_imshp[2], op_imshp[3])
        kshp = outshp
    elif node.op.border_mode == 'full':
        (img, filters) = (newtopgrad, newimg)
        kshp_logical = None
        kshp_logical_top_aligned = True
        imshp_logical = (op_imshp[0],
                         fulloutshp[0],
                         fulloutshp[1])
        (bsize, nkern) = (op_kshp[0], op_imshp[1])
        imshp = (op_imshp[0], outshp[0], outshp[1])
        kshp = op_imshp[2:]
    else:
        raise NotImplementedError(
            'Only [full,valid] modes are currently supported.')

    # Flip the kernels
    filters = filters[:, :, ::-1, ::-1]

    dw = ConvOp(imshp, kshp, nkern, bsize, 1, 1, output_mode='valid',
                unroll_batch=None, unroll_kern=None, unroll_patch=None,
                imshp_logical=imshp_logical,
                kshp_logical=kshp_logical,
                kshp_logical_top_aligned=kshp_logical_top_aligned,
                direction_hint='bprop weights')
    res = dw(img, filters)
    copy_stack_trace(node.outputs[0], res)

    if node.op.border_mode == 'valid':
        res = res.dimshuffle((1, 0, 2, 3))
        res = res[:, :, ::-1, ::-1]

    res = theano.tensor.patternbroadcast(res, node.outputs[0].broadcastable)

    copy_stack_trace(node.outputs[0], res)
    return [res]


@local_optimizer([AbstractConv2d_gradInputs])
def local_conv2d_gradinputs_cpu(node):
    if not isinstance(node.op, AbstractConv2d_gradInputs):
        return None

    kern, topgrad, shape = node.inputs

    if ((not isinstance(kern.type, TensorType) or
         not isinstance(topgrad.type, TensorType))):
        return None
    if node.op.border_mode not in ['full', 'valid']:
        return None
    if not node.op.filter_flip:
        # Not tested yet
        return None

    # Conv 3d implementation, needed when subsample > 2
    if node.op.border_mode == 'valid' and node.op.subsample != (1, 1):
        kern = kern[:, :, ::-1, ::-1]
        shuffled_kern = kern.dimshuffle(0, 2, 3, 'x', 1)
        shuffled_topgrad = topgrad.dimshuffle(0, 2, 3, 'x', 1)
        b = theano.tensor.zeros_like(shuffled_kern[0, 0, 0, 0, :])
        rval = convTransp3D(W=shuffled_kern, b=b,
                            d=(node.op.subsample[0], node.op.subsample[1], 1),
                            H=shuffled_topgrad,
                            RShape=(shape[0], shape[1], 1))
        copy_stack_trace(node.outputs[0], rval)
        rval = theano.tensor.addbroadcast(rval, 3)
        rval = rval.dimshuffle(0, 4, 1, 2)
        rval = theano.tensor.patternbroadcast(rval,
                                              node.outputs[0].broadcastable)

        copy_stack_trace(node.outputs[0], rval)
        return [rval]

    # Conv2d Implementation
    dx, dy = node.op.subsample
    if dx not in (1, 2) or dy not in (1, 2):
        # Not implemented in the gradient of ConvOp
        return None

    if node.op.imshp is None:
        op_imshp = (None, None, None, None)
    else:
        op_imshp = node.op.imshp

    if node.op.kshp is None:
        op_kshp = (None, None, None, None)
    else:
        op_kshp = node.op.kshp

    if None in op_imshp or None in op_kshp:
        if (dx, dy) != (1, 1):
            return None

    mode = 'valid'
    if not node.op.border_mode == 'full':
        mode = 'full'
    filters = kern.dimshuffle((1, 0, 2, 3))
    filters = filters[:, :, ::-1, ::-1]

    outshp = get_conv_output_shape(op_imshp, op_kshp,
                                   node.op.border_mode, node.op.subsample)[2:]
    fulloutshp = get_conv_output_shape(op_imshp, op_kshp,
                                       node.op.border_mode, (1, 1))[2:]

    nkern = op_imshp[1]
    imshp = (op_kshp[0], outshp[0], outshp[1])
    imshp_logical = (op_kshp[0], fulloutshp[0], fulloutshp[1])
    din = ConvOp(imshp,
                 op_kshp[2:],
                 nkern,
                 op_imshp[0],
                 1, 1, output_mode=mode,
                 unroll_batch=None, unroll_kern=None,
                 unroll_patch=None,
                 imshp_logical=imshp_logical,
                 kshp_logical=None,
                 version=-1,
                 direction_hint='bprop inputs')
    din = din(topgrad, filters)
    copy_stack_trace(node.outputs[0], din)
    din = theano.tensor.patternbroadcast(din, node.outputs[0].broadcastable)
    copy_stack_trace(node.outputs[0], din)
    return [din]


# Register Cpu Optmization
conv_groupopt = theano.gof.optdb.LocalGroupDB()
conv_groupopt.__name__ = "conv_opts"
register_specialize_device(conv_groupopt, 'fast_compile', 'fast_run')

# GEMM-based convolution
# It can be disabled by excluding 'conv_gemm'.
conv_groupopt.register('local_abstractconv_gemm', local_abstractconv_gemm, 30,
                       'conv_gemm', 'fast_compile', 'fast_run')
conv_groupopt.register('local_abstractconv_gradweight_gemm',
                       local_abstractconv_gradweight_gemm, 30,
                       'conv_gemm', 'fast_compile', 'fast_run')
conv_groupopt.register('local_abstractconv_gradinputs_gemm',
                       local_abstractconv_gradinputs_gemm, 30,
                       'conv_gemm', 'fast_compile', 'fast_run')
# Legacy convolution
conv_groupopt.register('local_conv2d_cpu', local_conv2d_cpu, 40,
                       'fast_compile', 'fast_run')
conv_groupopt.register('local_conv2d_gradweight_cpu',
                       local_conv2d_gradweight_cpu, 40,
                       'fast_compile', 'fast_run')
conv_groupopt.register('local_conv2d_gradinputs_cpu',
                       local_conv2d_gradinputs_cpu, 40,
                       'fast_compile', 'fast_run')
