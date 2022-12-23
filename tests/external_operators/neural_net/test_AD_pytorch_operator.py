import pytest

from firedrake import *
# from firedrake_adjoint import *

from firedrake.external_operators.neural_networks.backends import get_backend

from ufl.algorithms.ad import expand_derivatives

import torch.nn.functional as F
import torch.autograd.functional as torch_func
from torch.nn import Module, Linear


@pytest.fixture(scope='module')
def mesh():
    return UnitSquareMesh(10, 10)


@pytest.fixture(scope='module')
def V(mesh):
    return FunctionSpace(mesh, "CG", 1)


@pytest.fixture(scope='module')
def u(mesh, V):
    x, y = SpatialCoordinate(mesh)
    return Function(V).interpolate(sin(pi * x) * sin(pi * y))


@pytest.fixture
def nn(model, V):
    # What should we do for inputs_format?
    return neuralnet(model, function_space=V)


@pytest.fixture(params=['linear', 'auto_encoder'])
def model(request, V):
    n = V.dim()
    if request.param == 'linear':
        return Linear(n, n)
    elif request.param == 'auto_encoder':
        f = AutoEncoder(n)
        f.double()
        return f


class AutoEncoder(Module):
    """Build a simple toy model"""

    def __init__(self, n):
        super(AutoEncoder, self).__init__()
        self.n1 = n
        self.n2 = int(2*n/3)
        self.n3 = int(n/2)
        # Encoder/decoder layers
        self.encoder_1 = Linear(self.n1, self.n2)
        self.encoder_2 = Linear(self.n2, self.n3)
        self.decoder_1 = Linear(self.n3, self.n2)
        self.decoder_2 = Linear(self.n2, self.n1)

    def encode(self, x):
        return self.encoder_2(F.relu(self.encoder_1(x)))

    def decode(self, x):
        return self.decoder_2(F.relu(self.decoder_1(x)))

    def forward(self, x):
        # x: [batch_size, n]
        encoded = self.encode(x)
        # encoded: [batch_size, n3]
        hidden = F.relu(encoded)
        decoded = self.decode(hidden)
        # decoded: [batch_size, n]
        return F.relu(decoded)


def test_forward(u, nn):
    # Set PytorchOperator
    N = nn(u)
    # Get model
    model = N.model

    # Assemble NeuralNet operator
    assembled_N = assemble(N)

    # Convert from Firedrake to PyTorch
    pytorch_backend = get_backend()
    x_P = pytorch_backend.to_ml_backend(u)
    # Forward pass
    y_P = model(x_P)
    # Convert from PyTorch to Firedrake
    y_F = pytorch_backend.from_ml_backend(y_P, u.function_space())

    # Check
    assert np.allclose(y_F.dat.data_ro, assembled_N.dat.data_ro)


def test_jvp(u, nn):
    # Set PytorchOperator
    N = nn(u)
    # Get model
    model = N.model
    # Set δu
    V = N.function_space()
    δu = Function(V)
    δu.vector()[:] = np.random.rand(V.dim())

    # Symbolic compute: <∂N/∂u, δu>
    dN = action(derivative(N, u), δu)
    # Assemble
    dN = assemble(dN)

    # Convert from Firedrake to PyTorch
    pytorch_backend = get_backend()
    δu_P = pytorch_backend.to_ml_backend(δu)
    u_P = pytorch_backend.to_ml_backend(u)
    # Compute Jacobian-vector product with PyTorch
    _, jvp_exact = torch_func.jvp(lambda x: model(x), u_P, δu_P)

    # Check
    assert np.allclose(dN.dat.data_ro, jvp_exact.numpy())


def test_vjp(u, nn):
    # Set PytorchOperator
    N = nn(u)
    # Get model
    model = N.model
    # Set δN
    V = N.function_space()
    δN = Cofunction(V.dual())
    δN.vector()[:] = np.random.rand(V.dim())

    # Symbolic compute: <(∂N/∂u)*, δN>
    dNdu = expand_derivatives(derivative(N, u))
    dNdu = action(adjoint(dNdu), δN)
    # Assemble
    dN_adj = assemble(dNdu)
    # TODO: Fix above so that can directly write: dN_adj = assemble(action(adjoint(derivative(N, u)), δN))

    # Convert from Firedrake to PyTorch
    pytorch_backend = get_backend()
    δN_P = pytorch_backend.to_ml_backend(δN)
    u_P = pytorch_backend.to_ml_backend(u)
    # Compute vector-Jacobian product with PyTorch
    _, vjp_exact = torch_func.vjp(lambda x: model(x), u_P, δN_P)

    # Check
    assert np.allclose(dN_adj.dat.data_ro, vjp_exact.numpy())


def test_jacobian(u, nn):
    # Set PytorchOperator
    N = nn(u)
    # Get model
    model = N.model

    # Assemble Jacobian of N
    dN = assemble(derivative(N, u))

    # Convert from Firedrake to PyTorch
    pytorch_backend = get_backend()
    u_P = pytorch_backend.to_ml_backend(u, unsqueeze=False)
    # Compute Jacobian with PyTorch
    J = torch_func.jacobian(lambda x: model(x), u_P)

    # Check
    assert np.allclose(dN.petscmat[:, :], J.numpy())


def test_jacobian_adjoint(u, nn):
    # Set PytorchOperator
    N = nn(u)
    # Get model
    model = N.model

    # Assemble Jacobian adjoint of N
    dNdu = expand_derivatives(derivative(N, u))
    dNdu = adjoint(dNdu)
    dN_adj = assemble(dNdu)

    # Convert from Firedrake to PyTorch
    pytorch_backend = get_backend()
    u_P = pytorch_backend.to_ml_backend(u, unsqueeze=False)
    # Compute Jacobian with PyTorch
    J = torch_func.jacobian(lambda x: model(x), u_P)
    # Take Hermitian transpose
    J_adj = J.H

    # Check
    assert np.allclose(dN_adj.petscmat[:, :], J_adj.numpy())


"""
def test_backpropagation(u, nn):
    # Set PytorchOperator
    N = nn(u)
    # Get model
    model = N.model
    # Set δN
    V = N.function_space()
    δN = Cofunction(V.dual())
    δN.vector()[:] = np.random.rand(V.dim())

    # Get model parameters (θ_F is a `firedrake.PytorchParams` object)
    θ_F = N.operator_params()
    assert isinstance(θ_F, PytorchParams)
    # In fact θ_F is the last operand of N
    assert N.ufl_operands == (u, θ_F)

    # Symbolic compute: <(∂N/∂u)*, δN>
    dNdθ = expand_derivatives(derivative(N, θ_F))
    dNdθ = action(adjoint(dNdθ), δN)
    # Assemble
    dN_adj = assemble(dNdθ)
    # TODO: Fix above so that can directly write: dN_adj = assemble(action(adjoint(derivative(N, θ)), δN))

    N_params = N.operator_params()
    N = nn(f, *N_params)
    y = Cofunction(V.dual())
    y.vector()[:] = 1
    for m, p in zip(N_params, model.parameters()):
        # Symbolically compute the derivative ∂N/∂m
        dNdm = derivative(N, m)
        #  Symbolic operation: <∂N/∂m^{∗}, y>
        dNdm = expand_derivatives(dNdm)
        backprop_y = action(adjoint(dNdm), y)
        # Backpropagate
        backprop_extop = assemble(backprop_y)
        assert backprop_extop.ufl_shape == p.shape
        # Backpropagate with PyTorch directly
        f_torch = torch.tensor(f.dat.data_ro)
        y_torch = torch.tensor(y.dat.data_ro)
        output = model(f_torch.unsqueeze(0)).squeeze(0)
        backprop_autograd, = torch.autograd.grad(output, p, grad_outputs=[y_torch])
        assert np.allclose(backprop_autograd.detach().numpy(), backprop_extop.dat.data_ro)
"""
