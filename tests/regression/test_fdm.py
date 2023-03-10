import pytest
from firedrake import *

ksp = {
    "mat_type": "matfree",
    "ksp_type": "cg",
    "ksp_atol": 0.0E0,
    "ksp_rtol": 1.0E-8,
    "ksp_norm_type": "natural",
    "ksp_monitor": None,
}

coarse = {
    "mat_type": "aij",
    "ksp_type": "preonly",
    "pc_type": "cholesky",
}

fdmstar = {
    "pc_type": "python",
    "pc_python_type": "firedrake.P1PC",
    "pmg_mg_coarse": coarse,
    "pmg_mg_levels": {
        "ksp_type": "chebyshev",
        "ksp_norm_type": "none",
        "esteig_ksp_type": "cg",
        "esteig_ksp_norm_type": "natural",
        "ksp_chebyshev_esteig": "0.75,0.25,0.0,1.0",
        "pc_type": "python",
        "pc_python_type": "firedrake.FDMPC",
        "fdm": {
            "pc_type": "python",
            "pc_python_type": "firedrake.ASMExtrudedStarPC",
            "pc_star_mat_ordering_type": "nd",
            "pc_star_sub_sub_pc_type": "cholesky",
        }
    }
}

facetstar = {
    "pc_type": "python",
    "pc_python_type": "firedrake.FacetSplitPC",
    "facet_pc_type": "python",
    "facet_pc_python_type": "firedrake.FDMPC",
    "facet_fdm_pc_use_amat": False,
    "facet_fdm_pc_type": "fieldsplit",
    "facet_fdm_pc_fieldsplit_type": "symmetric_multiplicative",
    "facet_fdm_fieldsplit_0": {
        "ksp_type": "preonly",
        "pc_type": "icc",
    },
    "facet_fdm_fieldsplit_1": {
        "ksp_type": "preonly",
        "pc_type": "python",
        "pc_python_type": "firedrake.P1PC",
        "pmg_mg_coarse": coarse,
        "pmg_mg_levels": {
            "ksp_type": "chebyshev",
            "ksp_norm_type": "none",
            "esteig_ksp_type": "cg",
            "esteig_ksp_norm_type": "natural",
            "ksp_chebyshev_esteig": "0.75,0.25,0.0,1.0",
            "pc_type": "python",
            "pc_python_type": "firedrake.ASMExtrudedStarPC",
            "pc_star_mat_ordering_type": "nd",
            "pc_star_sub_sub_pc_type": "cholesky",
        }
    }
}

fdmstar.update(ksp)
facetstar.update(ksp)


def solve_riesz_map(V, d):
    beta = Constant(1E-4)
    subs = [(1, 3)]
    if V.mesh().cell_set._extruded:
        subs += ["top"]

    x = SpatialCoordinate(V.mesh())
    x -= Constant([0.5]*len(x))
    if V.ufl_element().value_shape() == ():
        u_exact = exp(-10*dot(x, x))
        u_bc = u_exact
    else:
        u_exact = x * exp(-10*dot(x, x))
        u_bc = Function(V)
        u_bc.project(u_exact, solver_parameters={"mat_type": "matfree", "pc_type": "jacobi"})

    bcs = [DirichletBC(V, u_bc, sub) for sub in subs]

    uh = Function(V)
    test = TestFunction(V)
    trial = TrialFunction(V)
    a = lambda v, u: inner(v, beta*u)*dx + inner(d(v), d(u))*dx
    problem = LinearVariationalProblem(a(test, trial), a(test, u_exact), uh, bcs=bcs)
    its = []
    for sparams in [fdmstar, facetstar]:
        uh.assign(0)
        solver = LinearVariationalSolver(problem, solver_parameters=sparams)
        solver.solve()
        its.append(solver.snes.ksp.getIterationNumber())
    return its


@pytest.fixture(params=[2, 3],
                ids=["Rectangle", "Box"])
def mesh(request):
    nx = 4
    distribution = {"overlap_type": (DistributedMeshOverlapType.VERTEX, 1)}
    m = UnitSquareMesh(nx, nx, quadrilateral=True, distribution_parameters=distribution)
    if request.param == 3:
        m = ExtrudedMesh(m, nx)

    x = SpatialCoordinate(m)
    xnew = as_vector([acos(1-2*xj)/pi for xj in x])
    m.coordinates.interpolate(xnew)
    return m


@pytest.fixture(params=[None, "fdm"], ids=["spectral", "fdm"])
def variant(request):
    return request.param


@pytest.mark.skipcomplex
def test_p_independence_hgrad(mesh):
    family = "Lagrange"
    expected = [9, 9] if mesh.topological_dimension() == 3 else [5, 5]
    for degree in range(3, 6):
        element = FiniteElement(family, cell=mesh.ufl_cell(), degree=degree, variant="fdm")
        V = FunctionSpace(mesh, element)
        assert solve_riesz_map(V, grad) <= expected


@pytest.mark.skipcomplex
def test_p_independence_hcurl(mesh):
    family = "NCE" if mesh.topological_dimension() == 3 else "RTCE"
    expected = [8, 7] if mesh.topological_dimension() == 3 else [4, 4]
    for degree in range(3, 6):
        element = FiniteElement(family, cell=mesh.ufl_cell(), degree=degree, variant="fdm")
        V = FunctionSpace(mesh, element)
        assert solve_riesz_map(V, curl) <= expected


@pytest.mark.skipcomplex
def test_p_independence_hdiv(mesh):
    family = "NCF" if mesh.topological_dimension() == 3 else "RTCF"
    expected = [3, 3]
    for degree in range(3, 6):
        element = FiniteElement(family, cell=mesh.ufl_cell(), degree=degree, variant="fdm")
        V = FunctionSpace(mesh, element)
        assert solve_riesz_map(V, div) <= expected


@pytest.mark.skipcomplex
def test_variable_coefficient(mesh):
    gdim = mesh.geometric_dimension()
    k = 4
    V = FunctionSpace(mesh, "Lagrange", k)
    u = TrialFunction(V)
    v = TestFunction(V)
    x = SpatialCoordinate(mesh)
    x -= Constant([0.5]*len(x))

    # variable coefficients
    alphas = [0.1+10*dot(x, x)]*gdim
    alphas[0] = 1+10*exp(-dot(x, x))
    alpha = diag(as_vector(alphas))
    beta = ((10*cos(3*pi*x[0]) + 20*sin(2*pi*x[1]))*cos(pi*x[gdim-1]))**2

    a = (inner(grad(v), dot(alpha, grad(u))) + inner(v, beta*u))*dx(degree=3*k+2)
    L = inner(v, Constant(1))*dx

    subs = ("on_boundary",)
    if mesh.cell_set._extruded:
        subs += ("top", "bottom")
    bcs = [DirichletBC(V, zero(V.ufl_element().value_shape()), sub) for sub in subs]

    uh = Function(V)
    problem = LinearVariationalProblem(a, L, uh, bcs=bcs)
    solver = LinearVariationalSolver(problem, solver_parameters=fdmstar)
    solver.solve()
    assert solver.snes.ksp.getIterationNumber() <= 14


@pytest.fixture(params=["cg", "dg", "rt"],
                ids=["cg", "dg", "rt"])
def fs(request, mesh):
    degree = 3
    tdim = mesh.topological_dimension()
    cell = mesh.ufl_cell()
    element = request.param
    variant = "fdm_ipdg"
    if element == "rt":
        family = "RTCF" if tdim == 2 else "NCF"
        return FunctionSpace(mesh, FiniteElement(family, cell, degree=degree, variant=variant))
    else:
        if tdim == 1:
            family = "DG" if element == "dg" else "CG"
        else:
            family = "DQ" if element == "dg" else "Q"
        return VectorFunctionSpace(mesh, FiniteElement(family, cell, degree=degree, variant=variant), dim=5-tdim)


@pytest.mark.skipcomplex
def test_ipdg_direct_solver(fs):
    mesh = fs.mesh()
    x = SpatialCoordinate(mesh)
    gdim = mesh.geometric_dimension()
    ncomp = fs.ufl_element().value_size()
    u_exact = dot(x, x)
    if ncomp:
        u_exact = as_vector([u_exact + Constant(k) for k in range(ncomp)])

    degree = fs.ufl_element().degree()
    try:
        degree, = set(degree)
    except TypeError:
        pass

    quad_degree = 2*(degree+1)-1
    uh = Function(fs)
    u = TrialFunction(fs)
    v = TestFunction(fs)

    # problem coefficients
    A1 = diag(Constant(range(1, gdim+1)))
    A2 = diag(Constant(range(1, ncomp+1)))
    alpha = lambda grad_u: dot(dot(A2, grad_u), A1)
    beta = diag(Constant(range(2, ncomp+2)))

    n = FacetNormal(mesh)
    f_exact = alpha(grad(u_exact))
    B = dot(beta, u_exact) - div(f_exact)
    T = dot(f_exact, n)

    extruded = mesh.cell_set._extruded
    subs = (1,)
    if gdim > 1:
        subs += (3,)
    if extruded:
        subs += ("top",)

    bcs = [DirichletBC(fs, u_exact, sub) for sub in subs]

    dirichlet_ids = subs
    if "on_boundary" in dirichlet_ids:
        neumann_ids = []
    else:
        make_tuple = lambda s: s if type(s) == tuple else (s,)
        neumann_ids = list(set(mesh.exterior_facets.unique_markers) - set(sum([make_tuple(s) for s in subs if type(s) != str], ())))
    if extruded:
        if "top" not in dirichlet_ids:
            neumann_ids.append("top")
        if "bottom" not in dirichlet_ids:
            neumann_ids.append("bottom")

    dxq = dx(degree=quad_degree, domain=mesh)
    if extruded:
        dS_int = dS_v(degree=quad_degree) + dS_h(degree=quad_degree)
        ds_ext = {"on_boundary": ds_v(degree=quad_degree), "bottom": ds_b(degree=quad_degree), "top": ds_t(degree=quad_degree)}
        ds_Dir = [ds_ext.get(s) or ds_v(s, degree=quad_degree) for s in dirichlet_ids]
        ds_Neu = [ds_ext.get(s) or ds_v(s, degree=quad_degree) for s in neumann_ids]
    else:
        dS_int = dS(degree=quad_degree)
        ds_ext = {"on_boundary": ds(degree=quad_degree)}
        ds_Dir = [ds_ext.get(s) or ds(s, degree=quad_degree) for s in dirichlet_ids]
        ds_Neu = [ds_ext.get(s) or ds(s, degree=quad_degree) for s in neumann_ids]

    ds_Dir = sum(ds_Dir, ds(tuple()))
    ds_Neu = sum(ds_Neu, ds(tuple()))
    eta = Constant((degree+1)**2)
    h = CellVolume(mesh)/FacetArea(mesh)
    penalty = eta/h

    outer_jump = lambda w, n: outer(w("+"), n("+")) + outer(w("-"), n("-"))
    num_flux = lambda w: alpha(avg(penalty/2) * outer_jump(w, n))
    num_flux_b = lambda w: alpha((penalty/2) * outer(w, n))

    a = (inner(v, dot(beta, u)) * dxq
         + inner(grad(v), alpha(grad(u))) * dxq
         + inner(outer_jump(v, n), num_flux(u)-avg(alpha(grad(u)))) * dS_int
         + inner(outer_jump(u, n), num_flux(v)-avg(alpha(grad(v)))) * dS_int
         + inner(outer(v, n), num_flux_b(u)-alpha(grad(u))) * ds_Dir
         + inner(outer(u, n), num_flux_b(v)-alpha(grad(v))) * ds_Dir)

    L = (inner(v, B)*dxq
         + inner(v, T)*ds_Neu
         + inner(outer(u_exact, n), 2*num_flux_b(v)-alpha(grad(v))) * ds_Dir)

    problem = LinearVariationalProblem(a, L, uh, bcs=bcs)
    solver = LinearVariationalSolver(problem, solver_parameters={
        "mat_type": "matfree",
        "ksp_type": "cg",
        "ksp_atol": 0.0E0,
        "ksp_rtol": 1.0E-8,
        "ksp_max_it": 3,
        "ksp_monitor": None,
        "ksp_norm_type": "unpreconditioned",
        "pc_type": "python",
        "pc_python_type": "firedrake.PoissonFDMPC",
        "fdm_pc_type": "cholesky",
        "fdm_pc_factor_mat_solver_type": "mumps",
        "fdm_pc_factor_mat_ordering_type": "nd",
    }, appctx={"eta": eta, })
    solver.solve()

    assert solver.snes.ksp.getIterationNumber() == 1
    assert norm(u_exact-uh, "H1") < 1.0E-8
