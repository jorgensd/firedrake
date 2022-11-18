from firedrake import *
from mpi4py import MPI
from netgen.geom2d import SplineGeometry
from netgen.occ import *
import netgen
import numpy as np
import pytest
import gc

def poisson(h, degree=2):
    comm = MPI.COMM_WORLD
    # Setting up Netgen geometry and mesh
    if comm.Get_rank() == 0:
        geo = SplineGeometry()
        geo.AddRectangle((0, 0), (np.pi, np.pi), bc="rect")
        ngmesh = geo.GenerateMesh(maxh=h)
        labels = ngmesh.GetBCIDs("rect")
    else:
        ngmesh = netgen.libngpy._meshing.Mesh(2)
        labels = None

    labels = comm.bcast(labels, root=0) 
    msh = Mesh(ngmesh)
    # Setting up the problem
    V = FunctionSpace(msh, "CG", degree)
    u = TrialFunction(V)
    v = TestFunction(V)
    f = Function(V)
    x, y = SpatialCoordinate(msh)
    f.interpolate(2*sin(x)*sin(y))
    a = inner(grad(u), grad(v))*dx
    l = inner(f, v) * dx
    u = Function(V)
    bc = DirichletBC(V, 0.0, labels)

    # Assembling matrix
    A = assemble(a, bcs=bc)
    b = assemble(l)
    bc.apply(b)

    # Solving the problem
    solve(A, u, b, solver_parameters={"ksp_type": "preonly", "pc_type": "lu"})

    # Computing the error
    f.interpolate(sin(x)*sin(y))
    return sqrt(assemble(inner(u - f, u - f) * dx)), u, f

def poisson3D(h, degree=2):
    comm = MPI.COMM_WORLD
    # Setting up Netgen geometry and mesh
    if comm.Get_rank() == 0:
        box = Box(Pnt(0, 0, 0), Pnt(np.pi, np.pi, np.pi))
        box.bc("bcs")
        geo = OCCGeometry(box)
        ngmesh = geo.GenerateMesh(maxh=h)
        labels = ngmesh.GetBCIDs("bcs")
    else:
        ngmesh = netgen.libngpy._meshing.Mesh(3)
        labels = None

    labels = comm.bcast(labels, root=0) 
    msh = Mesh(ngmesh)

    # Setting up the problem
    V = FunctionSpace(msh, "CG", degree)
    u = TrialFunction(V)
    v = TestFunction(V)
    f = Function(V)
    x, y, z = SpatialCoordinate(msh)
    f.interpolate(3*sin(x)*sin(y)*sin(z))
    a = inner(grad(u), grad(v))*dx
    l = inner(f, v) * dx
    u = Function(V)
    bc = DirichletBC(V, 0.0, labels)

    # Assembling matrix
    A = assemble(a, bcs=bc)
    b = assemble(l)
    bc.apply(b)

    # Solving the problem
    solve(A, u, b, solver_parameters={"ksp_type": "preonly", "pc_type": "lu"})

    # Computing the error
    f.interpolate(sin(x)*sin(y)*sin(z))
    S = sqrt(assemble(inner(u - f, u - f) * dx))
    return S

def test_firedrake_Poisson_netgen():
    diff = np.array([poisson(h)[0] for h in [1/2, 1/4, 1/8]])
    print("l2 error norms:", diff)
    conv = np.log2(diff[:-1] / diff[1:])
    print("convergence order:", conv)
    assert (np.array(conv) > 2.8).all()


def test_firedrake_Poisson3D_netgen():
    diff = np.array([poisson3D(h) for h in [2,1,1/2]])
    print("l2 error norms:", diff)
    conv = np.log2(diff[:-1] / diff[1:])
    print("convergence order:", conv)
    assert (np.array(conv) > 2.8).all()


def test_firedrake_Adaptivity_netgen():
    gc.collect()
    comm = MPI.COMM_WORLD
    from petsc4py import PETSc
    from slepc4py import SLEPc

    def Solve(msh,labels):
        V = FunctionSpace(msh, "CG", 2)
        u = TrialFunction(V)
        v = TestFunction(V)
        a = inner(grad(u), grad(v))*dx
        m = (u*v)*dx
        uh = Function(V)
        bc = DirichletBC(V, 0, labels)
        A = assemble(a, bcs=bc)
        M = assemble(m)
        Asc, Msc = A.M.handle, M.M.handle
        E = SLEPc.EPS().create()
        E.setType(SLEPc.EPS.Type.ARNOLDI)
        E.setProblemType(SLEPc.EPS.ProblemType.GHEP)
        E.setDimensions(1, SLEPc.DECIDE)
        E.setOperators(Asc, Msc)
        ST = E.getST()
        ST.setType(SLEPc.ST.Type.SINVERT)
        PC = ST.getKSP().getPC()
        PC.setType("lu")
        PC.setFactorSolverType("mumps")
        E.setST(ST)
        E.solve()
        vr, vi = Asc.getVecs()
        with uh.dat.vec_wo as vr:
            lam = E.getEigenpair(0, vr, vi)
        return (lam, uh, V)

    def Mark(msh, uh, lam):
        W = FunctionSpace(msh, "DG", 0)
        w = TestFunction(W)
        R_T = lam.real*uh + div(grad(uh))
        n = FacetNormal(V.mesh())
        h = CellDiameter(msh)
        R_dT = dot(grad(uh), n)
        eta = assemble(h**2*R_T**2*w*dx + (h("+")+h("-"))*(R_dT("+")-R_dT("-"))**2*(w("+")+w("-"))*dS)
        frac = .95
        delfrac = .05
        part = .2
        with eta.dat.vec as etaVec:
            markedVec = etaVec.duplicate()
            markedVec.set(0)
            sum_eta = etaVec.sum()
            if sum_eta < tolerance:
                return markedVec
            eta_max = etaVec.max()[1]
            sct, etaVec0 = PETSc.Scatter.toZero(etaVec)
            markedVec0 = etaVec0.duplicate()
            sct(etaVec,etaVec0)
            if etaVec.getComm().getRank() == 0:
                eta = etaVec0.getArray()
                marked = np.zeros(eta.size, dtype='bool')
                sum_marked_eta = 0.
                while sum_marked_eta < part*sum_eta:
                    new_marked = (~marked) & (eta > frac*eta_max)
                    sum_marked_eta += sum(eta[new_marked])
                    marked += new_marked
                    frac -= delfrac
                markedVec0.getArray()[:] = 1.0*marked[:]
            sct(markedVec0,markedVec,mode=PETSc.Scatter.Mode.REVERSE)
            return markedVec

    def Refine(msh, marked):
        marked0 = marked
        getIdx = msh._cell_numbering.getOffset
        if msh.sfBCInv is not None:
            getIdx = lambda x : x
            _, marked0 = msh.topology_dm.distributeField(msh.sfBCInv,msh._cell_numbering,marked)
        if comm.Get_rank() == 0: 
            mark = marked0.getArray()
            for i,el in enumerate(msh.netgen_mesh.Elements2D()):
                if mark[getIdx(i)]:
                    el.SetRefinementFlag(1)
                else:
                    el.SetRefinementFlag(0)
            msh.netgen_mesh.RefineFlaged(0, True)
            return msh.netgen_mesh
        else:
            return netgen.libngpy._meshing.Mesh(2)

    tolerance = 1e-16
    max_iterations = 15
    exact = 3.375610652693620492628**2
    geo = SplineGeometry()
    pnts = [(0, 0), (1, 0), (1, 1),
            (0, 1), (-1, 1), (-1, 0),
            (-1, -1), (0, -1)]
    p1, p2, p3, p4, p5, p6, p7, p8 = [geo.AppendPoint(*pnt) for pnt in pnts]
    curves = [[["line", p1, p2], "line"],
              [["spline3", p2, p3, p4], "curve"],
              [["spline3", p4, p5, p6], "curve"],
              [["spline3", p6, p7, p8], "curve"],
              [["line", p8, p1], "line"]]
    [geo.Append(c, bc=bc) for c, bc in curves]
    if comm.Get_rank() == 0:
        ngmsh = geo.GenerateMesh(maxh=0.2)
        labels = sum([ngmsh.GetBCIDs(label) for label in ["line", "curve"]], [])
    else:
        ngmsh = netgen.libngpy._meshing.Mesh(2)
        labels = None
    labels = comm.bcast(labels, root=0)
    msh = Mesh(ngmsh)
    for i in range(max_iterations):
        lam, uh, V = Solve(msh,labels)
        marked = Mark(msh, uh, lam)
        msh = Mesh(Refine(msh, marked))
    assert(abs(lam-exact)<1e-2)

