import collections
import numpy as np
import ufl

from pyop2 import op2
from pyop2.exceptions import DataTypeError, DataValueError
from firedrake.petsc import PETSc
from firedrake.utils import ScalarType
from ufl.formatting.ufl2unicode import ufl2unicode


import firedrake.utils as utils
from firedrake.adjoint.constant import ConstantMixin


__all__ = ['Constant']


def _globalify(value):
    data = np.array(value, dtype=ScalarType)
    shape = data.shape
    rank = len(shape)
    if rank == 0:
        dat = op2.Global(1, data)
    else:
        dat = op2.Global(shape, data)
    return dat, rank, shape


class Constant(ufl.Coefficient, ConstantMixin):

    """A "constant" coefficient

    A :class:`Constant` takes one value over the whole
    :func:`~.Mesh`. The advantage of using a :class:`Constant` in a
    form rather than a literal value is that the constant will be
    passed as an argument to the generated kernel which avoids the
    need to recompile the kernel if the form is assembled for a
    different value of the constant.

    :arg value: the value of the constant.  May either be a scalar, an
         iterable of values (for a vector-valued constant), or an iterable
         of iterables (or numpy array with 2-dimensional shape) for a
         tensor-valued constant.

    :arg domain: an optional :func:`~.Mesh` on which the constant is defined.

    .. note::

       If you intend to use this :class:`Constant` in a
       :class:`~ufl.form.Form` on its own you need to pass a
       :func:`~.Mesh` as the domain argument.
    """

    def __new__(cls, *args, **kwargs):
        # Hack to avoid hitting `ufl.Coefficient.__new__` which may perform operations
        # meant for coefficients and not constants (e.g. check if the function space is dual or not)
        # This is a consequence of firedrake.Constant inheriting from ufl.Constant instead of ufl.Coefficient.
        return object.__new__(cls)

    @ConstantMixin._ad_annotate_init
    def __init__(self, value, domain=None):
        # Init also called in mesh constructor, but constant can be built without mesh
        utils._init()
        self.dat, rank, shape = _globalify(value)

        cell = None
        if domain is not None:
            domain = ufl.as_domain(domain)
            cell = domain.ufl_cell()
        if rank == 0:
            e = ufl.FiniteElement("Real", cell, 0)
        elif rank == 1:
            e = ufl.VectorElement("Real", cell, 0, shape[0])
        else:
            e = ufl.TensorElement("Real", cell, 0, shape=shape)

        fs = ufl.FunctionSpace(domain, e)
        super(Constant, self).__init__(fs)
        self._repr = 'Constant(%r, %r)' % (self.ufl_element(), self.count())

    @PETSc.Log.EventDecorator()
    def evaluate(self, x, mapping, component, index_values):
        """Return the evaluation of this :class:`Constant`.

        :arg x: The coordinate to evaluate at (ignored).
        :arg mapping: A mapping (ignored).
        :arg component: The requested component of the constant (may
             be ``None`` or ``()`` to obtain all components).
        :arg index_values: ignored.
        """
        if component in ((), None):
            if self.ufl_shape == ():
                return self.dat.data_ro[0]
            return self.dat.data_ro
        return self.dat.data_ro[component]

    def values(self):
        """Return a (flat) view of the value of the Constant."""
        return self.dat.data_ro.reshape(-1)

    def function_space(self):
        """Return a null function space."""
        return None

    def split(self):
        return (self,)

    def cell_node_map(self, bcs=None):
        """Return a null cell to node map."""
        if bcs is not None:
            raise RuntimeError("Can't apply boundary conditions to a Constant")
        return None

    def interior_facet_node_map(self, bcs=None):
        """Return a null interior facet to node map."""
        if bcs is not None:
            raise RuntimeError("Can't apply boundary conditions to a Constant")
        return None

    def exterior_facet_node_map(self, bcs=None):
        """Return a null exterior facet to node map."""
        if bcs is not None:
            raise RuntimeError("Can't apply boundary conditions to a Constant")
        return None

    @PETSc.Log.EventDecorator()
    @ConstantMixin._ad_annotate_assign
    def assign(self, value):
        """Set the value of this constant.

        :arg value: A value of the appropriate shape"""
        try:
            self.dat.data = value
            return self
        except (DataTypeError, DataValueError) as e:
            raise ValueError(e)

    def __iadd__(self, o):
        raise NotImplementedError("Augmented assignment to %s not implemented" % str(type(self)))

    def __isub__(self, o):
        raise NotImplementedError("Augmented assignment to %s not implemented" % str(type(self)))

    def __imul__(self, o):
        raise NotImplementedError("Augmented assignment to %s not implemented" % str(type(self)))

    def __idiv__(self, o):
        raise NotImplementedError("Augmented assignment to %s not implemented" % str(type(self)))

    def __str__(self):
        return ufl2unicode(self)


class PytorchParams(Constant):

    """A "constant" coefficient

    A :class:`Constant` takes one value over the whole
    :func:`~.Mesh`. The advantage of using a :class:`Constant` in a
    form rather than a literal value is that the constant will be
    passed as an argument to the generated kernel which avoids the
    need to recompile the kernel if the form is assembled for a
    different value of the constant.

    :arg value: the value of the constant.  May either be a scalar, an
         iterable of values (for a vector-valued constant), or an iterable
         of iterables (or numpy array with 2-dimensional shape) for a
         tensor-valued constant.

    :arg domain: an optional :func:`~.Mesh` on which the constant is defined.

    .. note::

       If you intend to use this :class:`Constant` in a
       :class:`~ufl.form.Form` on its own you need to pass a
       :func:`~.Mesh` as the domain argument.
    """

    def __init__(self, params):
        from firedrake.external_operators.neural_networks.backends import get_backend
        self.backend = get_backend('pytorch')

        params = (params,) if not isinstance(params, collections.abc.Sequence) else params

        if not all([isinstance(θ, self.backend.nn.parameter.Parameter) for θ in params]):
            raise TypeError('Expecting parameters of type %s' % str(self.backend.nn.parameter.Parameter))

        self.params = params

    def __add__(self, value):
        if isinstance(value, collections.abc.Sequence) and all([isinstance(θ, self.backend.nn.parameter.Parameter) for θ in value]):
            # Delegate to Pytorch: 1) the `+` operation and 2) the compatibility check on parameters in `value`
            # If parameters in value don't match -> let PyTorch cause the detailed error message
            θ = []
            # Should the addition be annotated here ?
            # Is it the right thing to not make a new object ?
            # At least keeping the same PyTorch object is (I think) the right thing to do since we don't want
            # to do things behind PyTorch's back by making new objects
            for a, b in zip(self.params, value):
                # θi will have a Clone block on its tape: is that desired
                # How should AD policy be here ? Note that if we detach
                # then θi won't have requires_grad=True.
                θi = a.clone()
                θi += b
                θ.append(θi)
            return PytorchParams(θ)

        raise ValueError('Cannot add %s' % value)

    def __mul__(self, value):
        if isinstance(value, collections.abc.Sequence) and all([isinstance(θ, self.backend.nn.parameter.Parameter) for θ in value]):
            # Delegate to Pytorch: 1) the `*` operation and 2) the compatibility check on parameters in `value`
            # If parameters in value don't match -> let PyTorch cause the detailed error message
            θ = []
            # Same comments than for __add__
            for a, b in zip(self.params, value):
                θi = a.clone()
                θi *= b
                θ.append(θi)
            return PytorchParams(θ)

        raise ValueError('Cannot multiply %s' % value)

    def __str__(self):
        # Delegate to torch Parameter class
        return str(self.params)

    def evaluate(self, x, mapping, component, index_values):
        raise NotImplementedError("Evaluation of %s objects is not implemented" % str(type(self)))

    def values(self):
        raise NotImplementedError("Accessing values of %s objects is not implemented" % str(type(self)))

    def assign(self, value):
        raise NotImplementedError("Assignment of %s objects is not implemented" % str(type(self)))
