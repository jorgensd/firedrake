from firedrake import *
from firedrake_adjoint import *
import pytest


@pytest.fixture(params=['iadd', 'isub', 'imul', 'idiv'])
def op(request):
    return request.param


@pytest.fixture(params=[1, 2])
def order(request):
    return request.param


@pytest.fixture(params=[2, -1])
def power(request):
    return request.param


def test_replay(op, order, power):
    """
    Given source and target functions of some `order`,
    verify that replaying the tape associated with the
    augmented operators +=, -=, *= and /= gives the same
    result as a hand derivation.
    """
    mesh = UnitSquareMesh(4, 4)
    x, y = SpatialCoordinate(mesh)
    V = FunctionSpace(mesh, "CG", order)

    # Source and target functions
    s = interpolate(x + 1, V)
    t = interpolate(y + 1, V)
    s_orig = s.copy(deepcopy=True)
    t_orig = t.copy(deepcopy=True)
    control_s = Control(s)
    control_t = Control(t)

    # Apply the operator
    if op == 'iadd':
        t += s
    elif op == 'isub':
        t -= s
    elif op == 'imul':
        t *= s
    elif op == 'idiv':
        t /= s
    else:
        raise ValueError("Operator '{:s}' not recognised".format(op))

    # Construct some nontrivial reduced functional
    f = lambda X: X**power
    J = assemble(f(t)*dx)
    rf_s = ReducedFunctional(J, control_s)
    rf_t = ReducedFunctional(J, control_t)

    with stop_annotating():

        # Check for consistency with the same input
        assert np.isclose(rf_s(s_orig), rf_s(s_orig))
        assert np.isclose(rf_t(t_orig), rf_t(t_orig))

        # Check for consistency with different input
        ss = s_orig.copy(deepcopy=True)
        tt = t_orig.copy(deepcopy=True)
        if op == 'iadd':
            ss += ss
            tt += tt
        elif op == 'isub':
            ss -= ss
            tt -= tt
        elif op == 'imul':
            ss *= ss
            tt *= tt
        elif op == 'idiv':
            ss /= ss
            tt /= tt
        assert np.isclose(rf_s(t_orig), assemble(f(tt)*dx))
        assert np.isclose(rf_t(s_orig), assemble(f(ss)*dx))

    # Clear tape
    tape = get_working_tape()
    tape.clear_tape()
