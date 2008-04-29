"""A L{Result} to store L{numpy.ndarray} with basic accompanying L{Op}s"""
import sys # for sys.maxint
import inspect

import numpy

from copy import copy

from gof import Result, Op, utils, Destroyer, Viewer, AbstractFunctionError, Type, Result, Constant, Apply
import gof

import blas # for gemm, dot

import elemwise as s2t
import scalar as scal

from functools import partial


def as_tensor(x, name = None):
    if isinstance(x, gof.Apply):
        if len(x.outputs) != 1:
            raise ValueError("It is ambiguous which output of a multi-output Op has to be fetched.", x)
        else:
            x = x.outputs[0]
    if isinstance(x, Result):
        if not isinstance(x.type, Tensor):
            raise TypeError("Result type field must be a Tensor.", x, x.type)
        return x
    if isinstance(x, Constant):
        if not isinstance(x.type, Tensor):
            raise TypeError("Constant type field must be a Tensor.", x, x.type)
        return x
    try:
        return constant(x)
    except TypeError:
        raise
        raise TypeError("Cannot convert %s to Tensor" % x, type(x))


def constant(x):
    if not isinstance(x, numpy.ndarray):
        x = numpy.asarray(x)
    try:
        return TensorConstant(Tensor(dtype = x.dtype,
                                     broadcastable = [d == 1 for d in x.shape]), x)
    except:
        raise
        raise TypeError("Could not convert %s to Tensor" % _x, type(_x))


class Tensor(Type):
    """
    L{Type} representing L{numpy.ndarray} in Theano.

    @todo: At some point we should document a glossary, such as terms like
    broadcasting and shape.
    
    @type dtype: numpy dtype string such as 'int64' or 'float64' (among others)
    @type broadcastable: tuple or list or array of boolean values, whose length
      is the number of dimensions of the L{ndarray} represented by this Type.
    @ivar broadcastable: Each element of the broadcastable vector tells us
      something about the corresponding dimension:
        - False means the dimension can be anything.
        - True means  the dimension must be 1. Also, this dimension will be considered
          for L{broadcasting}, as described and implemented in Numpy.
    """

    def __init__(self, dtype, broadcastable):
        self.dtype = str(dtype)
        self.broadcastable = broadcastable
        self.dtype_specs() # error checking is done there
    
    def filter(self, data, strict = False):
        _data = data
        if strict:
            assert isinstance(data, numpy.ndarray)
            assert str(data.dtype) == self.dtype
        else:
            data = numpy.asarray(data, dtype = self.dtype)
        if not self.ndim == data.ndim:
            raise TypeError("Wrong number of dimensions: expected %s, got %s." % (self.ndim, data.ndim), _data)
        if any(b and d != 1 for d, b in zip(data.shape, self.broadcastable)):
            raise ValueError("Non-unit value on shape on a broadcastable dimension.", data.shape, self.broadcastable)
        return data

    def dtype_specs(self):
        """Return python - C type correspondance tuple for self.data

        Return a tuple (python type, c type, numpy typenum) that corresponds to
        L{self.dtype}.  It is for use in C code generation.
        """
        #TODO: add more type correspondances for e.g. int32, int64, float32,
        #complex64, etc.
        try:
            return {'float32': (float, 'npy_float32', 'NPY_FLOAT32'),
                    'float64': (float, 'npy_float64', 'NPY_FLOAT64'),
                    'int8': (int, 'npy_int8', 'NPY_INT8'),
                    'int16': (int, 'npy_int16', 'NPY_INT16'),
                    'int32': (int, 'npy_int32', 'NPY_INT32'),
                    'int64': (int, 'npy_int64', 'NPY_INT64'),
                    'complex128': (complex, 'theano_complex128', 'NPY_COMPLEX128'),
                    'complex64': (complex, 'theano_complex64', 'NPY_COMPLEX64')}[self.dtype]
        except KeyError:
            raise TypeError("Unsupported dtype for %s: %s" % (self.__class__.__name__, self.dtype))

    def __eq__(self, other):
        return type(self) == type(other) and other.dtype == self.dtype and other.broadcastable == self.broadcastable

    def __hash__(self):
        return hash(self.dtype) ^ hash(self.broadcastable)

    ndim = property(lambda self: len(self.broadcastable), doc = "read-only access to the number of dimensions")

    def make_result(self, name = None):
        return TensorResult(self, name = name)

    def __str__(self):
        return "%s(%s)" % (str(self.dtype), str(self.broadcastable))

    def __repr__(self):
        return "Tensor{%s, %s}" % (str(self.dtype), str(self.broadcastable))

    def c_declare(self, name, sub):
        return """
        PyArrayObject* %(name)s;
        int type_num_%(name)s;
        typedef %(dtype)s dtype_%(name)s;
        """ % dict(sub, name = name, dtype = self.dtype_specs()[1])

    def c_init(self, name, sub):
        return """
        %(name)s = NULL;
        type_num_%(name)s = %(type_num)s;
        """ % dict(sub, name = name, type_num = self.dtype_specs()[2])

    def c_extract(self, name, sub):
        return """
        %(name)s = NULL;
        type_num_%(name)s = %(type_num)s;
        if (py_%(name)s == Py_None) {
            // We can either fail here or set %(name)s to NULL and rely on Ops using
            // tensors to handle the NULL case, but if they fail to do so they'll end up
            // with nasty segfaults, so this is public service.
            PyErr_SetString(PyExc_ValueError, "expected an ndarray, not None");
            %(fail)s
            //%(name)s = NULL;
        }
        else if (!PyArray_Check(py_%(name)s)) {
            PyErr_SetString(PyExc_ValueError, "expected an ndarray");
            %(fail)s
        }
        else if (((PyArrayObject*)py_%(name)s)->descr->type_num != %(type_num)s) {
            PyErr_SetString(PyExc_ValueError, "expected %(type_num)s");
            %(fail)s
        }
        else {
            %(name)s = (PyArrayObject*)(py_%(name)s);
            Py_XINCREF(%(name)s);
        }
        """ % dict(sub, name = name, type_num = self.dtype_specs()[2])

    def c_cleanup(self, name, sub):
        return """
        if (%(name)s) {
            Py_XDECREF(%(name)s);
        }
        """ % locals()
    
    def c_sync(self, name, sub):
        return """
        if (!%(name)s) {
            Py_XDECREF(py_%(name)s);
            py_%(name)s = Py_None;
        }
        else if ((void*)py_%(name)s != (void*)%(name)s) {
            Py_XDECREF(py_%(name)s);
            py_%(name)s = (PyObject*)%(name)s;
            Py_XINCREF(py_%(name)s);
        }
        """ % locals()

    def c_headers(self):
        return []

    def c_libraries(self):
        return []

    def c_support_code(cls):
        template = """
        struct theano_complex%(nbits)s : public npy_complex%(nbits)s
        {
            typedef theano_complex%(nbits)s complex_type;
            typedef npy_float%(half_nbits)s scalar_type;

            complex_type operator +(complex_type y) {
                complex_type ret;
                ret.real = this->real + y.real;
                ret.imag = this->imag + y.imag;
                return ret;
            }
            complex_type operator -(complex_type y) {
                complex_type ret;
                ret.real = this->real - y.real;
                ret.imag = this->imag - y.imag;
                return ret;
            }
            complex_type operator *(complex_type y) {
                complex_type ret;
                ret.real = this->real * y.real - this->imag * y.imag;
                ret.imag = this->real * y.imag + this->imag * y.real;
                return ret;
            }
            complex_type operator /(complex_type y) {
                complex_type ret;
                scalar_type y_norm_square = y.real * y.real + y.imag * y.imag;
                ret.real = (this->real * y.real + this->imag * y.imag) / y_norm_square;
                ret.imag = (this->imag * y.real - this->real * y.imag) / y_norm_square;
                return ret;
            }
        };
        """
        return template % dict(nbits = 64, half_nbits = 32) + template % dict(nbits = 128, half_nbits = 64)
        # todo: use C templating


# Easy constructors

def _multi(*fns):
    def f2(f, names):
        if len(names) == 1:
            return f(names)
        else:
            return [f(name) for name in names]
    if len(fns) == 1:
        return partial(f2, fns)
    else:
        return [partial(f2, f) for f in fns]

fscalar = Tensor('float32', ())
dscalar = Tensor('float64', ())
iscalar = Tensor('int32', ())
lscalar = Tensor('int64', ())
def scalar(name = None, dtype = 'float64'):
    type = Tensor(dtype, ())
    return type(name)
scalars, fscalars, dscalars, iscalars, lscalars = _multi(scalar, fscalar, dscalar, iscalar, lscalar)

fvector = Tensor('float32', (False, ))
dvector = Tensor('float64', (False, ))
ivector = Tensor('int32', (False, ))
lvector = Tensor('int64', (False, ))
def vector(name = None, dtype = 'float64'):
    type = Tensor(dtype, (False, ))
    return type(name)
vectors, fvectors, dvectors, ivectors, lvectors = _multi(vector, fvector, dvector, ivector, lvector)

fmatrix = Tensor('float32', (False, False))
dmatrix = Tensor('float64', (False, False))
imatrix = Tensor('int32', (False, False))
lmatrix = Tensor('int64', (False, False))
def matrix(name = None, dtype = 'float64'):
    type = Tensor(dtype, (False, False))
    return type(name)
matrices, fmatrices, dmatrices, imatrices, lmatrices = _multi(matrix, fmatrix, dmatrix, imatrix, lmatrix)

frow = Tensor('float32', (True, False))
drow = Tensor('float64', (True, False))
irow = Tensor('int32', (True, False))
lrow = Tensor('int64', (True, False))
def row(name = None, dtype = 'float64'):
    type = Tensor(dtype, (True, False))
    return type(name)
rows, frows, drows, irows, lrows = _multi(row, frow, drow, irow, lrow)

fcol = Tensor('float32', (False, True))
dcol = Tensor('float64', (False, True))
icol = Tensor('int32', (False, True))
lcol = Tensor('int64', (False, True))
def col(name = None, dtype = 'float64'):
    type = Tensor(dtype, (False, True))
    return type(name)
cols, fcols, dcols, icols, lcols = _multi(col, fcol, dcol, icol, lcol)


class _tensor_py_operators:
    #UNARY
    def __abs__(self): return _abs(self)
    def __neg__(self): return neg(self)

    #CASTS
    def __int__(self): return AsInt(self).out
    def __float__(self): return AsInt(self).out
    def __complex__(self): return AsComplex(self).out

    #COMPARISONS
    def __lt__(self,other): return lt(self, other)
    def __le__(self,other): return le(self, other)
    def __gt__(self,other): return gt(self, other)
    def __ge__(self,other): return ge(self, other)

    #ARITHMETIC - NORMAL
    def __add__(self,other): return add(self,other)
    def __sub__(self,other): return sub(self,other)
    def __mul__(self,other): return mul(self,other)
    def __div__(self,other): return div(self,other)
    def __pow__(self,other): return pow(self,other)

    #ARITHMETIC - INPLACE
    def __iadd__(self,other): return add_inplace(self,other)
    def __isub__(self,other): return sub_inplace(self,other)
    def __imul__(self,other): return mul_inplace(self,other)
    def __idiv__(self,other): return div_inplace(self,other)
    def __ipow__(self,other): return pow_inplace(self,other)

    #ARITHMETIC - RIGHT-OPERAND
    def __radd__(self,other): return add(other,self)
    def __rsub__(self,other): return sub(other,self)
    def __rmul__(self,other): return mul(other,self)
    def __rdiv__(self,other): return div(other,self)
    def __rpow__(self,other): return pow(other,self)

    #TRANSPOSE
    T = property(lambda self: transpose(self))

    #SLICING
    def __getitem__(self, item): return subtensor(self, item)
    def __getslice__(self, *args): return subtensor(self, slice(*args))

    #COPYING
    def copy(self): return tensor_copy(self)

class TensorResult(Result, _tensor_py_operators):
    pass

class TensorConstant(Constant, _tensor_py_operators):
    pass

s2t.as_tensor = as_tensor    
s2t.Tensor = Tensor
s2t.TensorResult = TensorResult
s2t.TensorConstant = TensorConstant



############################
# Supporting Ops
############################

# this has a different name, because _as_tensor is the function which ops use
# to upcast their arguments... this internal-use function is a good place to put debugging stuff, better than the global astensor.
_as_tensor = as_tensor




# class _Op(Op):
#     """
#     A basic L{Op} subclass that can be used to make L{Op}s that operate on L{Tensor}s.
#     It is not mandatory to inherit from this class, but it is practical.

#     @ivar nin: number of inputs
#     @ivar nout: number of outputs
#     @ivar out_tensor_class: L{Tensor} subclass used to instantiate the outputs

#      - input_wrapper: returns a L{Tensor} from its argument
#      - propagate_dtype: returns a list of dtypes corresponding to the
#      output dtypes from a list of input dtypes (if an input is not a
#      L{Tensor}, the passed value will be None)
#      - propagate_broadcastable: returns a list of tuples corresponding
#      to the output broadcastable flags from the input broadcastable flags
#      (if an input is not a L{Tensor}, the passed value will be None).
#     """
    
#     nin = -1 # nin == -1 means: arbitrary number of inputs
#     nout = 1

#     def __init__(self, *inputs):
#         inputs = map(_as_tensor, inputs)
        
#         if self.nin >= 0:
#             if len(inputs) != self.nin:
#                 raise TypeError("Wrong number of inputs for %s (got %i, expected %i)") \
#                     % (self, len(inputs), self.nin)

#         i_broadcastables = [getattr(input, 'broadcastable', None) for input in inputs]
#         i_dtypes = [getattr(input, 'dtype', None) for input in inputs]

#         o_broadcastables = utils.from_return_values(self.propagate_broadcastable(*i_broadcastables))
#         o_dtypes = utils.from_return_values(self.propagate_dtype(*i_dtypes))

#         self.inputs = inputs
#         self.outputs = [Tensor(dtype, broadcastable) for broadcastable, dtype in zip(o_broadcastables, o_dtypes)]

#     def propagate_broadcastable(self, *inputs):
#         raise AbstractFunctionError()
    
#     def propagate_dtype(self, *i_dtypes):
#         rval = set([dtype for dtype in i_dtypes if dtype is not None])
#         if len(rval) == 0:
#             raise ValueError("Cannot infer the dtypes of the outputs with no Tensor inputs.")
#         elif len(rval) > 1:
#             raise ValueError("The dtypes of all inputs should be identical.")
#         return [rval.pop()] * self.nout



##########################
# Unary Operations
##########################

# def broadcast(scalar_opclass, name, module_name = None, inplace_versions = True):
#     C = s2t.make_broadcast(scalar_opclass, name = name, module_name = module_name) # this returns a class
#     C.__module__ = module_name
#     c = gof.op.constructor(s2t.wrap_broadcast(C))
#     if inplace_versions:
#         CInplace = s2t.make_broadcast(scalar_opclass, {0:0}, name = name+"Inplace")
#         CInplace.__module__ = module_name
#         c_inplace = gof.op.constructor(s2t.wrap_broadcast(CInplace))
#         return C, c, CInplace, c_inplace
#     else:
#         return C, c

# def _broadcast(scalar_opclass, name, inplace_versions = True):
#     return broadcast(scalar_opclass, name, 'tensor', inplace_versions)

    
# class Argmax(Op):
#     """Calculate the max and argmax over a given axis"""
#     nin=2 # tensor, axis
#     nout=2 # max val, max idx
#     E_axis = 'invalid axis'
#     debug = 0
#     def __init__(self, x, axis=None):
#         x = _as_tensor(x)
#         if axis is None:
#             axis = len(x.broadcastable) -1
#         axis = _as_tensor(axis)
#         self.inputs = [x, axis]
#         broadcastable = [0] * (len(x.broadcastable) - 1)
#         self.outputs = [Tensor(x.dtype, broadcastable), 
#                 Tensor(axis.dtype, broadcastable)]
#     def perform(self): 
#         axis = self.inputs[1].data
#         x = self.inputs[0].data
#         self.outputs[0].data = numpy.max(x, axis)
#         self.outputs[1].data = numpy.argmax(x,axis)
# argmax = gof.op.constructor(Argmax)

# def max(x, axis=None):
#     """Return maximum elements obtained by iterating over given axis

#     Default axis is the last one.
#     """
#     # In python (using Argmax.perform()) this leads to an wasteful
#     # implementation that goes through the data twice instead of once
#     # but when Argmax.c_impl() is in place, it should be fine.
#     return argmax(x,axis)[0]


def _elemwise(scalar_op, name):
    straight = s2t.Elemwise(scalar_op)
    inplace_scalar_op = scalar_op.__class__(scal.transfer_type(0))
    inplace = s2t.Elemwise(inplace_scalar_op, {0: 0})
    return straight, inplace

_abs, abs_inplace = _elemwise(scal.abs, 'abs')
exp, exp_inplace = _elemwise(scal.exp, 'exp')
neg, neg_inplace = _elemwise(scal.neg, 'neg')
log, log_inplace = _elemwise(scal.log, 'log')
log2, log2_inplace = _elemwise(scal.log2, 'log2')
sgn, sgn_inplace = _elemwise(scal.sgn, 'sgn')
sqr, sqr_inplace = _elemwise(scal.sqr, 'sqr')
sqrt, sqrt_inplace = _elemwise(scal.sqrt, 'sqrt')
cos, cos_inplace = _elemwise(scal.cos, 'cos')
sin, sin_inplace = _elemwise(scal.sin, 'sin')
tan, tan_inplace = _elemwise(scal.tan, 'tan')
cosh, cosh_inplace = _elemwise(scal.cosh, 'cosh')
sinh, sinh_inplace = _elemwise(scal.sinh, 'sinh')
tanh, tanh_inplace = _elemwise(scal.tanh, 'tanh')

fill, fill_inplace = _elemwise(scal.second, 'fill')

def ones_like(model):
    return fill(model, 1.0)
def zeros_like(model):
    return fill(model, 0.0)

tensor_copy = s2t.Elemwise(scal.identity)

def sum(input, axis = None):
    return s2t.Sum(axis)(input)


##########################
# Arithmetics
##########################

add, add_inplace = _elemwise(scal.add, 'add')
sub, sub_inplace = _elemwise(scal.sub, 'sub')
mul, mul_inplace = _elemwise(scal.mul, 'mul')
div, div_inplace = _elemwise(scal.div, 'div')
pow, pow_inplace = _elemwise(scal.pow, 'pow')


##########################
# View Operations
##########################

class TransposeInplace(Op):

    def make_node(self, input):
        return Apply(self, [input], [input.type()])
    
    def perform(self, node, (x, ), (z, )):
        z[0] = x.T
    
    def grad(self, (x,), (gz,)):
        return transpose(gz),
    
    def c_code(self, node, name, (x, ), (z, ), sub):
        return """
        PyArrayObject* transposed = (PyArrayObject*)PyArray_Transpose(%(x)s, NULL);
        if (%(z)s) {
            Py_XDECREF(%(z)s);
        }
        %(z)s = transposed;
        """ % locals()

transpose_inplace = TransposeInplace()
def transpose(x, **kwargs):
    return transpose_inplace(tensor_copy(x), **kwargs)

# class Subtensor(Op, Viewer):
#     nin = 2
#     nout = 1
#     e_invalid = 'invalid index'
#     debug = 0
#     def __init__(self, *args,**kwargs):
#         def as_tuple_result(obj):
#             if isinstance(obj, Result):
#                 return obj
#             r = gof.result.PythonResult(None)
#             if isinstance(obj, tuple):
#                 r.data = obj
#             else:
#                 r.data = (obj,)
#             return r
#         def pad(tplR, N):
#             l = list(tplR.data)
#             for i in range(len(l), N):
#                 l.append(slice(0,sys.maxint,1))
#             tplR.data = tuple(l)

#         if Subtensor.debug:
#             print 'Subtensor.__init__', args, kwargs
#         #Olivier says not to call this
#         #Op.__init__(self,  *args,**kwargs) 
#         #Viewer.__init__(self, *args,**kwargs)
#         t, coord = args
#         t = _as_tensor(t)
#         coord = as_tuple_result(coord)
#         if len(coord.data) > len(t.broadcastable):
#             raise ValueError(Subtensor.e_invalid)
#         # add the implicit extra unbounded slices 
#         # e.g. n[0] on a 3d tensor pads to n[0,:,:]
#         pad(coord, len(t.broadcastable))
#         broadcastable = [0 for c in coord.data if isinstance(c, slice)]
#         if Subtensor.debug:
#             print 'brdcstble', broadcastable
#             print 't', t.data
#             print 'coord', coord.data
#         self.inputs = [t, coord]
#         self.outputs = [Tensor(t.dtype, broadcastable)]
#     def view_map(self): 
#         return {self.out: [self.inputs[0]]}
#     def perform(self):
#         x = self.inputs[0].data
#         c = self.inputs[1].data
#         if Subtensor.debug:
#             print 'perform: x', x
#             print 'perform: c', c
#         if len(c) == 1:
#             self.outputs[0].data = x.__getitem__(c[0])
#         else:
#             self.outputs[0].data = x.__getitem__(c)
#     def grad(self, (x,), (gz,)):
#         # - option: allocate a potentially large matrix of zeros, and fill in
#         # the appropriate elements from gz
#         # - option: return a sparse matrix
#         # - option: return gz, but think about how to include a special addition
#         # function that works on a corresponding view of the original data
#         raise NotImplementedError() 
# subtensor = gof.op.constructor(Subtensor)


#########################
# Linalg : Dot
#########################

class Dot(Op):
#     nin=2
#     nout=1
    def make_node(self, *inputs):
        inputs = map(as_tensor, inputs)
        if len(inputs) != 2:
            raise TypeError("Wrong number of inputs for %s (got %i, expected 2)" % self)
        i_broadcastables = [input.type.broadcastable for input in inputs]
        i_dtypes = [input.type.dtype for input in inputs]

#         o_broadcastables = utils.from_return_values(self.propagate_broadcastable(*i_broadcastables))
#         o_dtypes = utils.from_return_values(self.propagate_dtype(*i_dtypes))

        bx, by = i_broadcastables
        if len(bx) == 0:     # x is a scalar
            bz = by
        else:
            if len(by) >= 2: #y is a matrix or tensor
                bz = bx[:-1] + by[:-2] + by[-1:]
            elif len(by)==1: #y is vector
                bz = bx[:-1]
            else:            #y is a scalar
                bz = bx
        o_broadcastables = [bz]
        o_dtypes = [scal.upcast(*i_dtypes)]

        outputs = [Tensor(t, b)() for b, t in zip(o_broadcastables, o_dtypes)]

        return Apply(self, inputs, outputs)
    def perform(self, node, (x, y), (z, )):
        z[0] = numpy.dot(x, y)
    def grad(self, (x, y), (gz,)):
        return dot(gz, y.T), dot(x.T, gz)
dot = Dot()

# class Gemm(_Op):
#     nin=5
#     nout=1
#     E_rank = 'gemm only works for rank 2'
#     E_scalar = 'gemm requires scalar argument'
#     E_z_uniq = 'argument z aliased to x or y'
#     debug = False
#     def __init__(self, *args, **kwargs):
#         _Op.__init__(self, *args, **kwargs)
#         z, a, x, y, b = self.inputs
#         zr, xr, yr = [set(gof.view_roots(i)) for i in z,x,y]
#         if zr.intersection(xr):
#             raise ValueError(Gemm.E_z_uniq, (z, x))
#         if zr.intersection(yr):
#             raise ValueError(Gemm.E_z_uniq, (z, y))
#     def destroy_map(self):
#         return {self.out:[self.inputs[0]]}
#     def propagate_broadcastable(self, bz, ba, bx, by, bb):
#         if len(bz) != 2: raise ValueError(Gemm.E_rank, len(bz))
#         if len(bx) != 2: raise ValueError(Gemm.E_rank, len(bx))
#         if len(by) != 2: raise ValueError(Gemm.E_rank, len(by))
#         if len(ba): raise ValueError(Gemm.E_scalar, ba)
#         if len(bb): raise ValueError(Gemm.E_scalar, bb)

#         return [bz]
#     def impl(self, z, a, x, y, b):
#         assert a.shape == ()
#         assert b.shape == ()
#         if z.shape == ():
#             z.itemset(z*a + b*numpy.dot(x,y))
#             return z
#         else:
#             if b == 0.0:
#                 if a == 1.0:
#                     z[:] = numpy.dot(x,y)
#                 elif a == -1.0:
#                     z[:] = -numpy.dot(x,y)
#                 else:
#                     z[:] = a * numpy.dot(x,y)
#             elif b == 1.0:
#                 if a == 1.0:
#                     z += numpy.dot(x,y)
#                 elif a == -1.0:
#                     z -= numpy.dot(x,y)
#                 else:
#                     z += a * numpy.dot(x,y)
#             else:
#                 z *= b
#                 z += a * numpy.dot(x,y)
#             return z
#     def grad(self, (z, a, x, y, b), (gz,)):
#         raise NotImplementedError()

#     def c_support_code(self):
#         #return blas.cblas_header_text()
#         mod_str = """
#         #ifndef MOD
#         #define MOD %
#         #endif
#         """
#         return blas.blas_proto() + mod_str
#     def c_headers(self):
#         return ['<iostream>']
#     def c_libraries(self):
#         return blas.ldflags()
#     def c_validate_update(self, *args):
#         return ""
#     def c_validate_update_cleanup(self, *args):
#         return ""
#     def c_code(self, (_z, _a, _x, _y, _b), (_zout, ), sub):
#         return """
#         int unit = 0;

#         int type_num = %(_x)s->descr->type_num;
#         int type_size = %(_x)s->descr->elsize; // in bytes

#         npy_intp* Nx = %(_x)s->dimensions;
#         npy_intp* Ny = %(_y)s->dimensions;
#         npy_intp* Nz = %(_z)s->dimensions;

#         npy_intp* Sx = %(_x)s->strides;
#         npy_intp* Sy = %(_y)s->strides;
#         npy_intp* Sz = %(_z)s->strides;

#         //strides for x, y, z in dimensions 0, 1
#         int sx_0, sx_1, sy_0, sy_1, sz_0, sz_1;

#         if (%(_zout)s != %(_z)s)
#         {
#             if (%(_zout)s)
#             {
#                 Py_DECREF(%(_zout)s);
#             }
#             %(_zout)s = %(_z)s;
#             Py_INCREF(%(_zout)s);
#         }

#         if (%(_x)s->nd != 2) {PyErr_SetString(PyExc_NotImplementedError, "rank(x) != 2"); %(fail)s;}
#         if (%(_y)s->nd != 2) {PyErr_SetString(PyExc_NotImplementedError, "rank(y) != 2"); %(fail)s;}
#         if (%(_z)s->nd != 2) {PyErr_SetString(PyExc_NotImplementedError, "rank(z) != 2"); %(fail)s;}

#         if ((%(_a)s->descr->type_num != PyArray_DOUBLE)
#             && (%(_a)s->descr->type_num != PyArray_FLOAT))
#         {PyErr_SetString(PyExc_NotImplementedError, "type(a) is not double or float"); %(fail)s;}

#         if ((%(_b)s->descr->type_num != PyArray_DOUBLE)
#             && (%(_b)s->descr->type_num != PyArray_FLOAT))
#         {PyErr_SetString(PyExc_NotImplementedError, "type(b) is not double or float"); %(fail)s;}

#         if ((%(_x)s->descr->type_num != PyArray_DOUBLE) 
#             && (%(_x)s->descr->type_num != PyArray_FLOAT))
#         {PyErr_SetString(PyExc_NotImplementedError, "type(x) is not double or float"); %(fail)s;}

#         if ((%(_y)s->descr->type_num != PyArray_DOUBLE) 
#             && (%(_y)s->descr->type_num != PyArray_FLOAT))
#         {PyErr_SetString(PyExc_NotImplementedError, "type(y) is not double or float"); %(fail)s;}

#         if ((%(_z)s->descr->type_num != PyArray_DOUBLE) 
#             && (%(_z)s->descr->type_num != PyArray_FLOAT))
#         {PyErr_SetString(PyExc_NotImplementedError, "type(z) is not double or float"); %(fail)s;}

#         if ((%(_x)s->descr->type_num != %(_y)s->descr->type_num)
#             ||(%(_x)s->descr->type_num != %(_z)s->descr->type_num))
#         { PyErr_SetString(PyExc_NotImplementedError, "type(z), type(y), type(z) are not all the same"); %(fail)s; }

#         if ((Nx[0] != Nz[0]) || (Nx[1] != Ny[0]) || (Ny[1] != Nz[1]))
#         {
#             PyErr_SetString(PyExc_ValueError, "Input dimensions do not agree");
#             %(fail)s;
#         }
#         if ((Sx[0] < 1) || (Sx[1] < 1) || (Sx[0] MOD type_size) || (Sx[1] MOD type_size)
#            || (Sy[0] < 1) || (Sy[1] < 1) || (Sy[0] MOD type_size) || (Sy[1] MOD type_size)
#            || (Sz[0] < 1) || (Sz[1] < 1) || (Sz[0] MOD type_size) || (Sz[1] MOD type_size))
#         {
#             PyErr_SetString(PyExc_ValueError, "stride is not multiple of element size"); %(fail)s;
#         }

#         /*
#         encode the stride structure of _x,_y,_z into a single integer
#         */
#         unit |= ((Sx[1] == type_size) ? 0x0 : (Sx[0] == type_size) ? 0x1 : 0x2) << 8;
#         unit |= ((Sy[1] == type_size) ? 0x0 : (Sy[0] == type_size) ? 0x1 : 0x2) << 4;
#         unit |= ((Sz[1] == type_size) ? 0x0 : (Sz[0] == type_size) ? 0x1 : 0x2) << 0;

#         /* create appropriate strides for malformed matrices that are row or column
#          * vectors
#          */
#         sx_0 = (Nx[0] > 1) ? Sx[0]/type_size : Nx[1];
#         sx_1 = (Nx[1] > 1) ? Sx[1]/type_size : Nx[0];
#         sy_0 = (Ny[0] > 1) ? Sy[0]/type_size : Ny[1];
#         sy_1 = (Ny[1] > 1) ? Sy[1]/type_size : Ny[0];
#         sz_0 = (Nz[0] > 1) ? Sz[0]/type_size : Nz[1];
#         sz_1 = (Nz[1] > 1) ? Sz[1]/type_size : Nz[0];

#         switch (type_num)
#         {
#             case PyArray_FLOAT:
#             {
#                 #define REAL float
#                 float a = (%(_a)s->descr->type_num == PyArray_FLOAT) 
#                 ? (REAL)(((float*)%(_a)s->data)[0])
#                 : (REAL)(((double*)%(_a)s->data)[0]);
#                 float b = (%(_b)s->descr->type_num == PyArray_FLOAT) ?
#                 (REAL)(((float*)%(_b)s->data)[0])
#                 : (REAL)(((double*)%(_b)s->data)[0]);

#                 float* x = (float*)PyArray_DATA(%(_x)s);
#                 float* y = (float*)PyArray_DATA(%(_y)s);
#                 float* z = (float*)PyArray_DATA(%(_z)s);
#                 char N = 'N';
#                 char T = 'T';
#                 int Nz0 = Nz[0], Nz1 = Nz[1], Nx1 = Nx[1];
#                 //std::cerr << (unit/256) MOD 16 << (unit / 16) MOD 16 << unit MOD 16<< '\\n';
#                 switch(unit)
#                 {
#                     case 0x000: sgemm_(&N, &N, &Nz1, &Nz0, &Nx1, &a, y, &sy_0, x, &sx_0, &b, z, &sz_0); break;
#                     case 0x100: sgemm_(&N, &T, &Nz1, &Nz0, &Nx1, &a, y, &sy_0, x, &sx_1, &b, z, &sz_0); break;
#                     case 0x010: sgemm_(&T, &N, &Nz1, &Nz0, &Nx1, &a, y, &sy_1, x, &sx_0, &b, z, &sz_0); break;
#                     case 0x110: sgemm_(&T, &T, &Nz1, &Nz0, &Nx1, &a, y, &sy_1, x, &sx_1, &b, z, &sz_0); break;
#                     case 0x001: sgemm_(&T, &T, &Nz0, &Nz1, &Nx1, &a, x, &sx_0, y, &sy_0, &b, z, &sz_1); break;
#                     case 0x101: sgemm_(&N, &T, &Nz0, &Nz1, &Nx1, &a, x, &sx_1, y, &sy_0, &b, z, &sz_1); break;
#                     case 0x011: sgemm_(&T, &N, &Nz0, &Nz1, &Nx1, &a, x, &sx_0, y, &sy_1, &b, z, &sz_1); break;
#                     case 0x111: sgemm_(&N, &N, &Nz0, &Nz1, &Nx1, &a, x, &sx_1, y, &sy_1, &b, z, &sz_1); break;
#                     default: PyErr_SetString(PyExc_ValueError, "some matrix has no unit stride"); %(fail)s;
#                 };
#                 #undef REAL
#             }
#             break;
#             case PyArray_DOUBLE:
#             {
#                 #define REAL double

#                 double a = (%(_a)s->descr->type_num == PyArray_FLOAT) 
#                 ? (REAL)(((float*)%(_a)s->data)[0])
#                 : (REAL)(((double*)%(_a)s->data)[0]);
#                 double b = (%(_b)s->descr->type_num == PyArray_FLOAT) ?
#                 (REAL)(((float*)%(_b)s->data)[0])
#                 : (REAL)(((double*)%(_b)s->data)[0]);
#                 double* x = (double*)PyArray_DATA(%(_x)s);
#                 double* y = (double*)PyArray_DATA(%(_y)s);
#                 double* z = (double*)PyArray_DATA(%(_z)s);
#                 char N = 'N';
#                 char T = 'T';
#                 int Nz0 = Nz[0], Nz1 = Nz[1], Nx1 = Nx[1];
#                 //std::cerr << (unit/256) MOD 16 << (unit / 16) MOD 16 << unit MOD 16<< '\\n';
#                 switch(unit)
#                 {
#                     case 0x000: dgemm_(&N, &N, &Nz1, &Nz0, &Nx1, &a, y, &sy_0, x, &sx_0, &b, z, &sz_0); break;
#                     case 0x100: dgemm_(&N, &T, &Nz1, &Nz0, &Nx1, &a, y, &sy_0, x, &sx_1, &b, z, &sz_0); break;
#                     case 0x010: dgemm_(&T, &N, &Nz1, &Nz0, &Nx1, &a, y, &sy_1, x, &sx_0, &b, z, &sz_0); break;
#                     case 0x110: dgemm_(&T, &T, &Nz1, &Nz0, &Nx1, &a, y, &sy_1, x, &sx_1, &b, z, &sz_0); break;
#                     case 0x001: dgemm_(&T, &T, &Nz0, &Nz1, &Nx1, &a, x, &sx_0, y, &sy_0, &b, z, &sz_1); break;
#                     case 0x101: dgemm_(&N, &T, &Nz0, &Nz1, &Nx1, &a, x, &sx_1, y, &sy_0, &b, z, &sz_1); break;
#                     case 0x011: dgemm_(&T, &N, &Nz0, &Nz1, &Nx1, &a, x, &sx_0, y, &sy_1, &b, z, &sz_1); break;
#                     case 0x111: dgemm_(&N, &N, &Nz0, &Nz1, &Nx1, &a, x, &sx_1, y, &sy_1, &b, z, &sz_1); break;
#                     default: PyErr_SetString(PyExc_ValueError, "some matrix has no unit stride"); %(fail)s;
#                 };
#                 #undef REAL
#             }
#             break;
#         }

#         """ % dict(locals(), **sub)
# gemm = gof.op.constructor(Gemm)

