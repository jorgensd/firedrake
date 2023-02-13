from functools import partial, lru_cache
from itertools import product
from pyop2.sparsity import get_preallocation
from firedrake.petsc import PETSc
from firedrake.preconditioners.base import PCBase
import firedrake.dmhooks as dmhooks
import firedrake
import numpy
import ufl
import ctypes
from firedrake_citations import Citations

Citations().add("Brubeck2021", """
@misc{Brubeck2021,
  title={A scalable and robust vertex-star relaxation for high-order {FEM}},
  author={Brubeck, Pablo D. and Farrell, Patrick E.},
  archiveprefix = {arXiv},
  eprint = {2107.14758},
  primaryclass = {math.NA},
  year={2021}
}
""")

__all__ = ("FDMPC", "PoissonFDMPC")


class FDMPC(PCBase):
    """
    A preconditioner for tensor-product elements that changes the shape
    functions so that the H(d) Riesz map is sparse on Cartesian cells,
    and assembles a global sparse matrix on which other preconditioners,
    such as `ASMStarPC`, can be applied.

    Here we assume that the volume integrals in the Jacobian can be expressed as:

    inner(d(v), alpha(d(u)))*dx + inner(v, beta(u))*dx

    where alpha and beta are linear functions (tensor contractions).
    The sparse matrix is obtained by approximating (v, alpha u) and (v, beta u) as
    diagonal mass matrices
    """

    _prefix = "fdm_"

    _variant = "fdm"

    _reference_tensor_cache = {}
    _coefficient_cache = {}
    _c_code_cache = {}

    @staticmethod
    def load_set_values(triu=False):
        cache = FDMPC._c_code_cache
        key = triu
        if key not in cache:
            comm = PETSc.COMM_SELF
            cache[key] = load_assemble_csr(comm, triu=triu)
        return cache[key]

    @PETSc.Log.EventDecorator("FDMInit")
    def initialize(self, pc):
        from firedrake.assemble import allocate_matrix, assemble
        from firedrake.preconditioners.pmg import prolongation_matrix_matfree
        from firedrake.preconditioners.patch import bcdofs
        Citations().register("Brubeck2021")

        self.comm = pc.comm
        Amat, Pmat = pc.getOperators()
        prefix = pc.getOptionsPrefix()
        options_prefix = prefix + self._prefix
        options = PETSc.Options(options_prefix)

        use_amat = options.getBool("pc_use_amat", True)
        pmat_type = options.getString("mat_type", PETSc.Mat.Type.AIJ)
        diagonal_scale = options.getBool("diagonal_scale", False)

        appctx = self.get_appctx(pc)
        fcp = appctx.get("form_compiler_parameters")
        self.appctx = appctx

        # Get original Jacobian form and bcs
        if Pmat.getType() == "python":
            ctx = Pmat.getPythonContext()
            J = ctx.a
            bcs = tuple(ctx.bcs)
            mat_type = "matfree"
        else:
            ctx = dmhooks.get_appctx(pc.getDM())
            J = ctx.Jp or ctx.J
            bcs = tuple(ctx._problem.bcs)
            mat_type = ctx.mat_type

        if isinstance(J, firedrake.slate.Add):
            J = J.children[0].form
        assert type(J) == ufl.Form

        # Transform the problem into the space with FDM shape functions
        V = J.arguments()[-1].function_space()
        element = V.ufl_element()
        e_fdm = element.reconstruct(variant=self._variant)

        if element == e_fdm:
            V_fdm, J_fdm, bcs_fdm = (V, J, bcs)
        else:
            # Matrix-free assembly of the transformed Jacobian
            V_fdm = firedrake.FunctionSpace(V.mesh(), e_fdm)
            J_fdm = J(*[t.reconstruct(function_space=V_fdm) for t in J.arguments()], coefficients={})
            bcs_fdm = tuple(bc.reconstruct(V=V_fdm, g=0) for bc in bcs)

            self.fdm_interp = prolongation_matrix_matfree(V, V_fdm, [], bcs_fdm)
            self.work_vec_x = Amat.createVecLeft()
            self.work_vec_y = Amat.createVecRight()
            if use_amat:
                omat = Amat
                self.A = allocate_matrix(J_fdm, bcs=bcs_fdm, form_compiler_parameters=fcp,
                                         mat_type=mat_type, options_prefix=options_prefix)
                self._assemble_A = partial(assemble, J_fdm, tensor=self.A, bcs=bcs_fdm,
                                           form_compiler_parameters=fcp, mat_type=mat_type)
                self._assemble_A()
                Amat = self.A.petscmat

                def interp_nullspace(I, nsp):
                    if not nsp.handle:
                        return nsp
                    vectors = []
                    for x in nsp.getVecs():
                        y = I.createVecLeft()
                        I.mult(x, y)
                        vectors.append(y)
                    if nsp.hasConstant():
                        y = I.createVecLeft()
                        x = I.createVecRight()
                        x.set(1.0E0)
                        I.mult(x, y)
                        vectors.append(y)
                        x.destroy()
                    return PETSc.NullSpace().create(constant=False, vectors=vectors, comm=nsp.getComm())

                inject = prolongation_matrix_matfree(V_fdm, V, [], [])
                Amat.setNullSpace(interp_nullspace(inject, omat.getNullSpace()))
                Amat.setTransposeNullSpace(interp_nullspace(inject, omat.getTransposeNullSpace()))
                Amat.setNearNullSpace(interp_nullspace(inject, omat.getNearNullSpace()))

            if len(bcs) > 0:
                self.bc_nodes = numpy.unique(numpy.concatenate([bcdofs(bc, ghost=False) for bc in bcs]))
            else:
                self.bc_nodes = numpy.empty(0, dtype=PETSc.IntType)

        self._ctx_ref = self.new_snes_ctx(pc, J_fdm, bcs_fdm, mat_type,
                                          fcp=fcp, options_prefix=options_prefix)

        # Assemble the FDM preconditioner with sparse local matrices
        Pmat, self._assemble_P = self.assemble_fdm_op(V_fdm, J_fdm, bcs_fdm, fcp, appctx, pmat_type, diagonal_scale)
        self._assemble_P()
        Pmat.setNullSpace(Amat.getNullSpace())
        Pmat.setTransposeNullSpace(Amat.getTransposeNullSpace())
        Pmat.setNearNullSpace(Amat.getNearNullSpace())

        # Internally, we just set up a PC object that the user can configure
        # however from the PETSc command line.  Since PC allows the user to specify
        # a KSP, we can do iterative by -fdm_pc_type ksp.
        fdmpc = PETSc.PC().create(comm=pc.comm)
        fdmpc.incrementTabLevel(1, parent=pc)

        # We set a DM and an appropriate SNESContext on the constructed PC so one
        # can do e.g. multigrid or patch solves.
        fdm_dm = V_fdm.dm
        self._dm = fdm_dm

        fdmpc.setDM(fdm_dm)
        fdmpc.setOptionsPrefix(options_prefix)
        fdmpc.setOperators(A=Amat, P=Pmat)
        fdmpc.setUseAmat(use_amat)
        self.pc = fdmpc
        with dmhooks.add_hooks(fdm_dm, self, appctx=self._ctx_ref, save=False):
            fdmpc.setFromOptions()

    def assemble_fdm_op(self, V, J, bcs, form_compiler_parameters, appctx, pmat_type, diagonal_scale):
        """
        Assemble the sparse preconditioner with cell-wise constant coefficients.

        :arg V: the :class:`firedrake.FunctionSpace` of the form arguments
        :arg J: the Jacobian bilinear form
        :arg bcs: an iterable of boundary conditions on V
        :arg appctx: the application context

        :returns: 2-tuple with the preconditioner :class:`PETSc.Mat` and its assembly callable
        """
        ifacet, = numpy.nonzero([is_restricted(Vsub.finat_element)[1] for Vsub in V])
        if len(ifacet) == 0:
            Vfacet = None
            Vbig = V
            _, fdofs = split_dofs(V.finat_element)
        elif len(ifacet) == 1:
            Vfacet = V[ifacet[0]]
            ebig, = set(unrestrict_element(Vsub.ufl_element()) for Vsub in V)
            Vbig = firedrake.FunctionSpace(V.mesh(), ebig)
            if len(V) > 1:
                dims = [Vsub.finat_element.space_dimension() for Vsub in V]
                assert sum(dims) == Vbig.finat_element.space_dimension()
            fdofs = restricted_dofs(Vfacet.finat_element, Vbig.finat_element)
        else:
            raise ValueError("Expecting at most one FunctionSpace restricted onto facets.")

        value_size = Vbig.value_size
        if value_size != 1:
            fdofs = numpy.add.outer(value_size * fdofs, numpy.arange(value_size, dtype=fdofs.dtype))
        dofs = numpy.arange(value_size * Vbig.finat_element.space_dimension(), dtype=fdofs.dtype)
        idofs = numpy.setdiff1d(dofs, fdofs, assume_unique=True)
        self.ises = tuple(PETSc.IS().createGeneral(indices, comm=PETSc.COMM_SELF) for indices in (idofs, fdofs))
        self.submats = [None for _ in range(7)]

        self.reference_tensor_on_diag = dict()
        self.get_static_condensation = dict()
        if Vfacet:
            # If we are in a facet space, we build the Schur complement on its diagonal block
            self.reference_tensor_on_diag[Vfacet] = self.assemble_reference_tensor(Vbig)
            self.get_static_condensation[Vfacet] = lambda A: condense_element_mat(A, self.ises[0], self.ises[1], self.submats)

        elif len(fdofs) and V.finat_element.formdegree == 0:
            # If we are in H(grad), we just pad with zeros on the statically-condensed pattern
            i1 = PETSc.IS().createGeneral(dofs, comm=PETSc.COMM_SELF)
            self.get_static_condensation[V] = lambda Ae: condense_element_pattern(Ae, self.ises[0], i1, self.submats)

        # dict of cell to global mappings for each function space
        self.cell_to_global = dict()
        self.lgmaps = dict()

        def cell_to_global(lgmap, cell_to_local, cell_index, result=None):
            result = cell_to_local(cell_index, result=result)
            return lgmap.apply(result, result=result)

        bc_rows = dict()
        for Vsub in V:
            lgmap = Vsub.local_to_global_map([bc.reconstruct(V=Vsub, g=0) for bc in bcs])
            bsize = Vsub.dof_dset.layout_vec.getBlockSize()
            cell_to_local, nel = glonum_fun(Vsub.cell_node_map(), bsize=bsize)
            self.cell_to_global[Vsub] = partial(cell_to_global, lgmap, cell_to_local)
            self.lgmaps[Vsub] = lgmap

            own = Vsub.dof_dset.layout_vec.getLocalSize()
            bdofs = numpy.nonzero(lgmap.indices[:own] < 0)[0].astype(PETSc.IntType)
            bc_rows[Vsub] = Vsub.dof_dset.lgmap.apply(bdofs, result=bdofs)

        # get coefficients on a given cell
        coefficients, assembly_callables = self.assemble_coef(J, form_compiler_parameters)
        coefs = [coefficients.get(k) for k in ("beta", "alpha")]
        coef_maps = [glonum_fun(ck.cell_node_map())[0] for ck in coefs]

        def get_coefs(e, result=None):
            vals = []
            for coef, cmap, indices in zip(coefs, coef_maps, get_coefs.indices):
                indices = cmap(e, result=indices)
                vals.append(coef.dat.data_ro[indices])
            return numpy.concatenate(vals, out=result)
        get_coefs.indices = [None for _ in range(len(coefs))]
        self.get_coefs = get_coefs

        self.nel = nel
        self.work_mats = dict()

        Pmats = dict()
        addv = PETSc.InsertMode.ADD_VALUES
        symmetric = pmat_type.endswith("sbaij")

        # Store only off-diagonal blocks with more columns than rows to save memory
        Vsort = sorted(V, key=lambda Vsub: Vsub.dim())
        for Vrow, Vcol in product(Vsort, Vsort):
            if symmetric and (Vcol, Vrow) in Pmats:
                P = PETSc.Mat().createTranspose(Pmats[Vcol, Vrow])
            else:
                on_diag = Vrow == Vcol
                triu = on_diag and symmetric
                ptype = pmat_type if on_diag else PETSc.Mat.Type.AIJ
                sizes = tuple(Vsub.dof_dset.layout_vec.getSizes() for Vsub in (Vrow, Vcol))
                # bsizes = tuple(Vsub.dof_dset.layout_vec.getBlockSize() for Vsub in (Vrow, Vcol))

                preallocator = PETSc.Mat().create(comm=self.comm)
                preallocator.setType(PETSc.Mat.Type.PREALLOCATOR)
                preallocator.setSizes(sizes)
                preallocator.setOption(PETSc.Mat.Option.IGNORE_ZERO_ENTRIES, False)
                preallocator.setUp()
                self.set_values(preallocator, Vrow, Vcol, addv, triu=triu)
                preallocator.assemble()
                d_nnz, o_nnz = get_preallocation(preallocator, sizes[0][0])
                preallocator.destroy()
                if on_diag:
                    numpy.maximum(d_nnz, 1, out=d_nnz)

                P = PETSc.Mat().create(comm=self.comm)
                P.setType(ptype)
                P.setSizes(sizes)
                # P.setBlockSizes(*bsizes)
                P.setPreallocationNNZ((d_nnz, o_nnz))
                P.setOption(PETSc.Mat.Option.NEW_NONZERO_ALLOCATION_ERR, True)
                if ptype.endswith("sbaij"):
                    P.setOption(PETSc.Mat.Option.IGNORE_LOWER_TRIANGULAR, True)
                P.setUp()
            Pmats[Vrow, Vcol] = P

        if len(V) == 1:
            Pmat = Pmats[V, V]
        else:
            Pmat = PETSc.Mat().createNest([[Pmats[Vrow, Vcol] for Vcol in V] for Vrow in V], comm=V.comm)

        self.diag = None

        def assemble_P():
            for _assemble in assembly_callables:
                _assemble()
            for Vrow, Vcol in product(Vsort, Vsort):
                P = Pmats[Vrow, Vcol]
                if P.getType().endswith("aij"):
                    P.zeroEntries()
                    if Vrow == Vcol and len(bc_rows[Vrow]) > 0:
                        rows = bc_rows[Vrow][:, None]
                        vals = numpy.ones(rows.shape, dtype=PETSc.RealType)
                        P.setValuesRCV(rows, rows, vals, addv)
                        del vals
                    self.set_values(P, Vrow, Vcol, addv)
            Pmat.assemble()
            if diagonal_scale:
                diag = Pmat.getDiagonal(result=self.diag)
                diag.sqrtabs()
                diag.reciprocal()
                Pmat.diagonalScale(L=diag, R=diag)
                self.diag = diag

        return Pmat, assemble_P

    @PETSc.Log.EventDecorator("FDMUpdate")
    def update(self, pc):
        if hasattr(self, "A"):
            self._assemble_A()
        self._assemble_P()

    def apply(self, pc, x, y):
        dm = self._dm
        with dmhooks.add_hooks(dm, self, appctx=self._ctx_ref):
            if hasattr(self, "fdm_interp"):
                self.fdm_interp.multTranspose(x, self.work_vec_x)
                self.pc.apply(self.work_vec_x, self.work_vec_y)
                self.fdm_interp.mult(self.work_vec_y, y)
                y.array_w[self.bc_nodes] = x.array_r[self.bc_nodes]
            else:
                self.pc.apply(x, y)

    def applyTranspose(self, pc, x, y):
        dm = self._dm
        with dmhooks.add_hooks(dm, self, appctx=self._ctx_ref):
            if hasattr(self, "fdm_interp"):
                self.fdm_interp.multTranspose(x, self.work_vec_y)
                self.pc.applyTranspose(self.work_vec_y, self.work_vec_x)
                self.fdm_interp.mult(self.work_vec_x, y)
                y.array_w[self.bc_nodes] = x.array_r[self.bc_nodes]
            else:
                self.pc.applyTranspose(x, y)

    def view(self, pc, viewer=None):
        super(FDMPC, self).view(pc, viewer)
        if hasattr(self, "pc"):
            viewer.printfASCII("PC to apply inverse\n")
            self.pc.view(viewer)

    def destroy(self, pc):
        objs = []
        if hasattr(self, "pc"):
            objs.append(self.pc.getOperators()[-1])
            objs.append(self.pc)
        if hasattr(self, "submats"):
            objs.extend(self.submats)
        if hasattr(self, "work_mats"):
            objs.extend(list(self.work_mats.values()))
        if hasattr(self, "ises"):
            objs.extend(self.ises)
        for obj in objs:
            if hasattr(obj, "destroy"):
                obj.destroy()

    @PETSc.Log.EventDecorator("FDMSetValues")
    def set_values(self, A, Vrow, Vcol, addv, triu=False):

        def RtAP(R, A, P, result=None):
            RtAP.buff = A.matMult(P, result=RtAP.buff)
            return R.transposeMatMult(RtAP.buff, result=result)
        RtAP.buff = None

        set_values_csr = self.load_set_values(triu=triu)
        get_rindices = self.cell_to_global[Vrow]
        if Vrow == Vcol:
            get_cindices = lambda e, result=None: result
            update_A = lambda Ae, rindices, cindices: set_values_csr(A, Ae, rindices, rindices, addv)
            rtensor = self.reference_tensor_on_diag.get(Vrow, None) or self.assemble_reference_tensor(Vrow)
            assemble_element_mat = lambda De, result=None: De.PtAP(rtensor, result=result)
            condense_element_mat = self.get_static_condensation.get(Vrow, None)
        else:
            get_cindices = self.cell_to_global[Vcol]
            update_A = lambda Ae, rindices, cindices: set_values_csr(A, Ae, rindices, cindices, addv)
            rtensor = self.assemble_reference_tensor(Vrow)
            ctensor = self.assemble_reference_tensor(Vcol)
            assemble_element_mat = lambda De, result=None: RtAP(rtensor, De, ctensor, result=result)
            condense_element_mat = None

        do_sort = True
        if condense_element_mat is None:
            condense_element_mat = lambda x: x
            do_sort = False

        common_key = "coefs"
        rindices = None
        cindices = None
        if A.getType() != PETSc.Mat.Type.PREALLOCATOR:
            Ae = self.work_mats[Vrow, Vcol]
            De = self.work_mats[common_key]
            data = self.work_csr[2]
            for e in range(self.nel):
                rindices = get_rindices(e, result=rindices)
                cindices = get_cindices(e, result=cindices)

                data = self.get_coefs(e, result=data)
                De.setValuesCSR(*self.work_csr)
                De.assemble()
                Ae = assemble_element_mat(De, result=Ae)
                update_A(condense_element_mat(Ae), rindices, cindices)

        elif self.nel:
            if common_key not in self.work_mats:
                data = self.get_coefs(0)
                data.fill(1.0E0)
                shape = data.shape + (1,)*(3-len(data.shape))
                nrows = shape[0] * shape[1]
                ai = numpy.arange(nrows+1, dtype=PETSc.IntType)
                aj = numpy.tile(ai[:-1].reshape((-1, shape[1])), (1, shape[2]))
                if shape[2] > 1:
                    ai *= shape[2]
                    data = numpy.tile(numpy.eye(shape[2]), shape[:1] + (1,)*(len(shape)-1))

                self.work_csr = (ai, aj, data)
                De = PETSc.Mat().createAIJ((nrows, nrows), csr=self.work_csr, comm=PETSc.COMM_SELF)
                self.work_mats[common_key] = De

            De = self.work_mats[common_key]
            Ae = assemble_element_mat(De, result=None)
            self.work_mats[Vrow, Vcol] = Ae
            if do_sort:
                sort_interior_dofs(self.ises[0], Ae)
            Se = condense_element_mat(Ae)

            for e in range(self.nel):
                rindices = get_rindices(e, result=rindices)
                cindices = get_cindices(e, result=cindices)
                update_A(Se, rindices, cindices)
        else:
            self.work_csr = (None, None, None)
            self.work_mats[common_key] = None
            self.work_mats[Vrow, Vcol] = None
        del rindices
        del cindices
        if RtAP.buff:
            RtAP.buff.destroy()

    def assemble_coef(self, J, form_compiler_parameters):
        """
        Obtain coefficients as the diagonal of a weighted mass matrix in V^k x V^{k+1}
        """
        from ufl.algorithms.ad import expand_derivatives
        from ufl.algorithms.expand_indices import expand_indices
        from firedrake.formmanipulation import ExtractSubBlock
        from firedrake.assemble import assemble

        index = len(J.arguments()[-1].function_space())-1
        if index:
            splitter = ExtractSubBlock()
            J = splitter.split(J, argument_indices=(index, index))

        mesh = J.ufl_domain()
        ndim = mesh.topological_dimension()
        args_J = J.arguments()
        e = args_J[0].ufl_element()
        if isinstance(e, (ufl.VectorElement, ufl.TensorElement)):
            e = e._sub_element
        e = unrestrict_element(e)
        sobolev = e.sobolev_space()

        map_grad = None
        if sobolev == ufl.H1:
            map_grad = lambda p: p
        elif sobolev in [ufl.HCurl, ufl.HDiv]:
            u = ufl.Coefficient(ufl.FunctionSpace(mesh, e))
            du = ufl.variable(ufl.grad(u))
            dku = ufl.div(u) if sobolev == ufl.HDiv else ufl.curl(u)
            eps = expand_derivatives(ufl.diff(ufl.replace(expand_derivatives(dku), {ufl.grad(u): du}), du))
            if sobolev == ufl.HDiv:
                map_grad = lambda p: ufl.outer(p, eps/ndim)
            elif len(eps.ufl_shape) == 3:
                map_grad = lambda p: ufl.dot(p, eps/2)
            else:
                map_grad = lambda p: p*(eps/2)

        V = args_J[0].function_space()
        formdegree = V.finat_element.formdegree
        degree = e.degree()
        try:
            degree = max(degree)
        except TypeError:
            pass
        qdeg = degree
        if formdegree == ndim:
            qfam = "DG" if ndim == 1 else "DQ"
            qdeg = 0
        elif formdegree == 0:
            qfam = "DG" if ndim == 1 else "RTCE" if ndim == 2 else "NCE"
        elif formdegree == 1 and ndim == 3:
            qfam = "NCF"
        else:
            qfam = "DQ L2"
            qdeg = degree-1

        qvariant = "fdm_quadrature"
        elements = [e.reconstruct(variant=qvariant),
                    ufl.FiniteElement(qfam, cell=mesh.ufl_cell(), degree=qdeg, variant=qvariant)]
        elements = list(map(ufl.BrokenElement, elements))
        if V.shape:
            elements = [ufl.TensorElement(ele, shape=V.shape) for ele in elements]

        Z = firedrake.FunctionSpace(mesh, ufl.MixedElement(elements))
        args = (firedrake.TestFunctions(Z), firedrake.TrialFunctions(Z))
        repargs = {t: v[0] for t, v in zip(args_J, args)}
        repgrad = {ufl.grad(t): map_grad(v[1]) for t, v in zip(args_J, args)} if map_grad else dict()
        Jcell = expand_indices(expand_derivatives(ufl.Form(J.integrals_by_type("cell"))))
        mixed_form = ufl.replace(ufl.replace(Jcell, repgrad), repargs)
        key = (mixed_form.signature(), mesh)
        block_diagonal = True

        if key not in self._coefficient_cache and False:
            M = assemble(mixed_form, mat_type="matfree",
                         form_compiler_parameters=form_compiler_parameters)

            coefs = []
            mats = []
            for iset in Z.dof_dset.field_ises:
                Msub = M.petscmat.createSubMatrix(iset, iset)
                coefs.append(Msub.getPythonContext()._diagonal)
                mats.append(Msub)

            def scale_coefficients():
                for Msub, coef in zip(mats, coefs):
                    ksp = PETSc.KSP().create(comm=V.comm)
                    ksp.setOperators(A=Msub, P=Msub)
                    ksp.setType(PETSc.KSP.Type.CG)
                    ksp.setNormType(PETSc.KSP.NormType.NATURAL)
                    ksp.pc.setType(PETSc.PC.Type.JACOBI)
                    ksp.setTolerances(rtol=1E-3, atol=0.0E0, max_it=8)
                    ksp.setComputeEigenvalues(True)
                    ksp.setUp()

                    x = Msub.createVecRight()
                    b = Msub.createVecLeft()
                    x.set(0)
                    b.setRandom()
                    ksp.solve(b, x)
                    ew = numpy.real(ksp.computeEigenvalues())
                    ksp.destroy()
                    x.destroy()
                    b.destroy()
                    dscale = (max(ew) + min(ew))/2
                    dscale = sum(ew) / len(ew)
                    scale = dscale if dscale == dscale else 1
                    with coef.dat.vec as diag:
                        diag.scale(scale)

            coefficients = {"beta": coefs[0], "alpha": coefs[1]}
            assembly_callables = [scale_coefficients]
            self._coefficient_cache[key] = (coefficients, assembly_callables)
            return self._coefficient_cache[key]

        if key not in self._coefficient_cache:
            if not block_diagonal or not V.shape:
                tensor = firedrake.Function(Z)
                beta, alpha = tensor.subfunctions
                coefficients = {"beta": beta, "alpha": alpha}
                assembly_callables = [partial(assemble, mixed_form, tensor=tensor, diagonal=True,
                                              form_compiler_parameters=form_compiler_parameters)]
            else:
                M = assemble(mixed_form, mat_type="matfree",
                             form_compiler_parameters=form_compiler_parameters)
                coefficients = dict()
                assembly_callables = []
                for iset, name in zip(Z.dof_dset.field_ises, ("beta", "alpha")):
                    sub = M.petscmat.createSubMatrix(iset, iset)
                    ctx = sub.getPythonContext()
                    coefficients[name] = ctx._block_diagonal
                    assembly_callables.append(ctx._assemble_block_diagonal)

            self._coefficient_cache[key] = (coefficients, assembly_callables)
        return self._coefficient_cache[key]

    @PETSc.Log.EventDecorator("FDMRefTensor")
    def assemble_reference_tensor(self, V):
        import FIAT
        ndim = V.mesh().topological_dimension()
        value_size = V.value_size
        formdegree = V.finat_element.formdegree
        degree = V.finat_element.degree
        try:
            degree = max(degree)
        except TypeError:
            pass
        if formdegree == ndim:
            degree = degree+1
        is_interior, is_facet = is_restricted(V.finat_element)
        key = (degree, ndim, formdegree, V.value_size, is_interior, is_facet)
        cache = self._reference_tensor_cache
        if key not in cache:
            full_key = (degree, ndim, formdegree, V.value_size, 0, 0)

            if is_facet and full_key in cache:
                result = cache[full_key]
                noperm = PETSc.IS().createGeneral(numpy.arange(result.getSize()[0], dtype=PETSc.IntType), comm=result.comm)
                cache[key] = result.createSubMatrix(noperm, self.ises[1])
                noperm.destroy()
                return cache[key]

            elements = sorted(get_base_elements(V.finat_element), key=lambda e: e.formdegree)
            cell = elements[0].get_reference_element()
            eq = FIAT.FDMQuadrature(cell, degree)
            e0 = elements[0] if elements[0].formdegree == 0 else FIAT.FDMLagrange(cell, degree)
            e1 = elements[-1] if elements[-1].formdegree == 1 else FIAT.FDMDiscontinuousLagrange(cell, degree-1)
            if hasattr(e0.dual, "rule"):
                rule = e0.dual.rule
            else:
                rule = FIAT.quadrature.make_quadratture(cell, degree+1)
            if is_interior:
                e0 = FIAT.RestrictedElement(e0, restriction_domain="interior")

            pts = rule.get_points()
            wts = rule.get_weights()

            phiq = eq.tabulate(0, pts)
            phi1 = e1.tabulate(0, pts)
            phi0 = e0.tabulate(1, pts)

            moments = lambda v, u: numpy.dot(numpy.multiply(v, wts), u.T)
            A00 = moments(phiq[(0, )], phi0[(0, )])
            A11 = moments(phi1[(0, )], phi1[(0, )])
            A10 = moments(phi1[(0, )], phi0[(1, )])
            A10 = numpy.linalg.solve(A11, A10)
            A11 = numpy.eye(A11.shape[0])

            Ihat = mass_matrix(ndim, formdegree, A00, A11)
            Dhat = diff_matrix(ndim, formdegree, A00, A11, A10)
            result = block_mat([[Ihat], [Dhat]])
            Ihat.destroy()
            Dhat.destroy()

            if value_size != 1:
                eye = petsc_sparse(numpy.eye(value_size))
                temp = result
                result = temp.kron(eye)
                temp.destroy()
                eye.destroy()

            if is_facet:
                cache[full_key] = result
                noperm = PETSc.IS().createGeneral(numpy.arange(result.getSize()[0], dtype=PETSc.IntType), comm=result.comm)
                result = result.createSubMatrix(noperm, self.ises[1])
                noperm.destroy()

            cache[key] = result
        return cache[key]


def factor_interior_mat(A00):
    # Assume that interior DOF list i0 is ordered such that A00 is block diagonal
    # with blocks of increasing dimension
    indptr, indices, data = A00.getValuesCSR()
    degree = numpy.diff(indptr)

    # TODO handle non-symmetric case with LU, requires scipy
    invchol = lambda X: numpy.linalg.inv(numpy.linalg.cholesky(X))
    nblocks = numpy.count_nonzero(degree == 1)
    zlice = slice(0, nblocks)
    numpy.sqrt(data[zlice], out=data[zlice])
    numpy.reciprocal(data[zlice], out=data[zlice])
    PETSc.Log.logFlops(2*nblocks)
    for k in range(2, degree[-1]+1):
        nblocks = numpy.count_nonzero(degree == k)
        zlice = slice(zlice.stop, zlice.stop + k*nblocks)
        data[zlice] = invchol(data[zlice].reshape((-1, k, k))).reshape((-1,))
        flops = ((k+1)**3 + 5*(k+1)-12)//3 + k**3
        PETSc.Log.logFlops(flops*nblocks)

    A00.setValuesCSR(indptr, indices, data)
    A00.assemble()
    del degree
    del indptr
    del indices
    del data


@PETSc.Log.EventDecorator("FDMCondense")
def condense_element_mat(A, i0, i1, submats):
    isrows = [i0, i0, i1, i1]
    iscols = [i0, i1, i0, i1]
    submats[:4] = A.createSubMatrices(isrows, iscols=iscols, submats=submats[:4] if submats[0] else None)
    A00, A01, A10, A11 = submats[:4]
    factor_interior_mat(A00)
    submats[4] = A00.matMult(A01, result=submats[4])
    submats[5] = A10.matTransposeMult(A00, result=submats[5])
    submats[6] = submats[5].matMult(submats[4], result=submats[6])
    submats[6].aypx(-1.0, A11)
    return submats[6]


@PETSc.Log.EventDecorator("FDMCondense")
def condense_element_pattern(A, i0, i1, submats):
    isrows = [i0, i0, i1]
    iscols = [i0, i1, i0]
    submats[:3] = A.createSubMatrices(isrows, iscols=iscols, submats=submats[:3] if submats[0] else None)
    A00, A01, A10 = submats[:3]
    submats[4] = A10.matTransposeMult(A00, result=submats[4])
    submats[5] = A00.matMult(A01, result=submats[5])
    submats[6] = submats[4].matMult(submats[5], result=submats[6])
    submats[6].aypx(0.0, A)
    return submats[6]


@PETSc.Log.EventDecorator("LoadCode")
def load_c_code(code, name, **kwargs):
    from pyop2.compilation import load
    from pyop2.utils import get_petsc_dir
    cppargs = ["-I%s/include" % d for d in get_petsc_dir()]
    ldargs = (["-L%s/lib" % d for d in get_petsc_dir()]
              + ["-Wl,-rpath,%s/lib" % d for d in get_petsc_dir()]
              + ["-lpetsc", "-lm"])
    funptr = load(code, "c", name,
                  cppargs=cppargs, ldargs=ldargs,
                  **kwargs)

    def get_pointer(obj):
        if isinstance(obj, (PETSc.Mat, PETSc.Vec)):
            return obj.handle
        elif isinstance(obj, numpy.ndarray):
            return obj.ctypes.data
        return obj

    @PETSc.Log.EventDecorator(name)
    def wrapper(*args):
        return funptr(*list(map(get_pointer, args)))
    return wrapper


def load_assemble_csr(comm, triu=False):
    if triu:
        name = "setSubMatCSR_SBAIJ"
        select_cols = "icol < irow ? -1: icol"
    else:
        name = "setSubMatCSR_AIJ"
        select_cols = "icol"
    code = f"""
#include <petsc.h>

PetscErrorCode {name}(Mat A,
                      Mat B,
                      PetscInt *rindices,
                      PetscInt *cindices,
                      InsertMode addv)
{{
    PetscInt ncols, irow, icol;
    PetscInt *cols, *indices;
    PetscScalar *vals;

    PetscInt m, n;
    PetscErrorCode ierr;
    PetscFunctionBeginUser;
    MatGetSize(B, &m, NULL);

    n = 0;
    for (PetscInt i = 0; i < m; i++) {{
        ierr = MatGetRow(B, i, &ncols, NULL, NULL);CHKERRQ(ierr);
        n = ncols > n ? ncols : n;
        ierr = MatRestoreRow(B, i, &ncols, NULL, NULL);CHKERRQ(ierr);
    }}
    PetscMalloc1(n, &indices);
    for (PetscInt i = 0; i < m; i++) {{
        ierr = MatGetRow(B, i, &ncols, &cols, &vals);CHKERRQ(ierr);
        irow = rindices[i];
        for (PetscInt j = 0; j < ncols; j++) {{
            icol = cindices[cols[j]];
            indices[j] = {select_cols};
        }}
        ierr = MatSetValues(A, 1, &irow, ncols, indices, vals, addv);CHKERRQ(ierr);
        ierr = MatRestoreRow(B, i, &ncols, &cols, &vals);CHKERRQ(ierr);
    }}
    PetscFree(indices);
    PetscFunctionReturn(0);
}}
"""
    argtypes = [ctypes.c_voidp, ctypes.c_voidp,
                ctypes.c_voidp, ctypes.c_voidp, ctypes.c_int]
    return load_c_code(code, name, comm=comm, argtypes=argtypes,
                       restype=ctypes.c_int)


def petsc_sparse(A_numpy, rtol=1E-10):
    Amax = max(A_numpy.min(), A_numpy.max(), key=abs)
    atol = rtol*Amax
    nnz = numpy.count_nonzero(abs(A_numpy) > atol, axis=1).astype(PETSc.IntType)
    A = PETSc.Mat().createAIJ(A_numpy.shape, nnz=(nnz, 0), comm=PETSc.COMM_SELF)
    for row, Arow in enumerate(A_numpy):
        cols = numpy.argwhere(abs(Arow) > atol).astype(PETSc.IntType).flat
        A.setValues(row, cols, Arow[cols], PETSc.InsertMode.INSERT)
    A.assemble()
    return A


def block_mat(A_blocks):
    if len(A_blocks) == 1:
        if len(A_blocks[0]) == 1:
            return A_blocks[0][0]

    nrows = sum([Arow[0].size[0] for Arow in A_blocks])
    ncols = sum([Aij.size[1] for Aij in A_blocks[0]])
    nnz = numpy.concatenate([sum([numpy.diff(Aij.getValuesCSR()[0]) for Aij in Arow]) for Arow in A_blocks])
    A = PETSc.Mat().createAIJ((nrows, ncols), nnz=(nnz, 0), comm=PETSc.COMM_SELF)
    imode = PETSc.InsertMode.INSERT
    insert_block = FDMPC.load_set_values()
    rsizes = [sum([Ai[0].size[0] for Ai in A_blocks[:k]]) for k in range(len(A_blocks)+1)]
    csizes = [sum([Aij.size[1] for Aij in A_blocks[0][:k]]) for k in range(len(A_blocks[0])+1)]
    rows = [numpy.arange(*rsizes[i:i+2], dtype=PETSc.IntType) for i in range(len(A_blocks))]
    cols = [numpy.arange(*csizes[j:j+2], dtype=PETSc.IntType) for j in range(len(A_blocks[0]))]
    for Ai, irows in zip(A_blocks, rows):
        for Aij, jcols in zip(Ai, cols):
            insert_block(A, Aij, irows, jcols, imode)

    A.assemble()
    for row in rows:
        del row
    for col in cols:
        del col
    return A


def is_restricted(finat_element):
    is_interior = True
    is_facet = True
    ndim = finat_element.cell.get_spatial_dimension()
    entity_dofs = finat_element.entity_dofs()
    for key in entity_dofs:
        v = sum(list(entity_dofs[key].values()), [])
        if len(v):
            edim = key
            try:
                edim = sum(edim)
            except TypeError:
                pass
            if edim == ndim:
                is_facet = False
            else:
                is_interior = False
    return is_interior, is_facet


def restricted_dofs(celem, felem):
    """
    find which DOFs from felem are on celem

    :arg celem: the restricted :class:`finat.FiniteElement`
    :arg felem: the unrestricted :class:`finat.FiniteElement`

    :returns: :class:`numpy.array` with indices of felem that correspond to celem
    """
    csplit = split_dofs(celem)
    fsplit = split_dofs(felem)
    if len(csplit[0]) and len(csplit[1]):
        csplit = [numpy.concatenate(csplit)]
        fsplit = [numpy.concatenate(fsplit)]

    k = len(csplit[0]) == 0
    if len(csplit[k]) != len(fsplit[k]):
        raise ValueError("Finite elements have different DOFs")
    perm = numpy.empty_like(csplit[k])
    perm[csplit[k]] = numpy.arange(len(perm), dtype=perm.dtype)
    return fsplit[k][perm]


def split_dofs(elem):
    entity_dofs = elem.entity_dofs()
    ndim = elem.cell.get_spatial_dimension()
    edofs = [[], []]
    for key in entity_dofs:
        vals = entity_dofs[key]
        edim = key
        try:
            edim = sum(edim)
        except TypeError:
            pass
        for k in vals.keys():
            edofs[edim < ndim].extend(sorted(vals[k]))
    return tuple(numpy.array(e, dtype=PETSc.IntType) for e in edofs)


def sort_interior_dofs(idofs, A):
    Aii = A.createSubMatrix(idofs, idofs)
    indptr, indices, _ = Aii.getValuesCSR()
    n = idofs.getSize()
    visit = numpy.zeros((n, ), dtype=bool)
    perm = []
    degree = 0
    while not visit.all():
        degree += 1
        for i in range(n):
            if not visit[i]:
                neigh = indices[slice(*indptr[i:i+2])]
                if len(neigh) == degree:
                    visit[neigh] = True
                    perm.extend(neigh)

    idofs.setIndices(idofs.getIndices()[perm])


def kron3(A, B, C, scale=None):
    temp = B.kron(C)
    if scale is not None:
        temp.scale(scale)
    result = A.kron(temp)
    temp.destroy()
    return result


def mass_matrix(ndim, formdegree, B00, B11):
    B00 = petsc_sparse(B00)
    B11 = petsc_sparse(B11)
    if ndim == 1:
        B_blocks = [B11 if formdegree else B00]
    elif ndim == 2:
        if formdegree == 0:
            B_blocks = [B00.kron(B00)]
        elif formdegree == 1:
            B_blocks = [B00.kron(B11), B11.kron(B00)]
        else:
            B_blocks = [B11.kron(B11)]
    elif ndim == 3:
        if formdegree == 0:
            B_blocks = [kron3(B00, B00, B00)]
        elif formdegree == 1:
            B_blocks = [kron3(B00, B00, B11), kron3(B00, B11, B00), kron3(B11, B00, B00)]
        elif formdegree == 2:
            B_blocks = [kron3(B00, B11, B11), kron3(B11, B00, B11), kron3(B11, B11, B00)]
        else:
            B_blocks = [kron3(B11, B11, B11)]

    B00.destroy()
    B11.destroy()
    if len(B_blocks) == 1:
        result = B_blocks[0]
    else:
        nrows = sum(Bk.size[0] for Bk in B_blocks)
        ncols = sum(Bk.size[1] for Bk in B_blocks)
        csr_block = [Bk.getValuesCSR() for Bk in B_blocks]
        ishift = numpy.cumsum([0] + [csr[0][-1] for csr in csr_block])
        jshift = numpy.cumsum([0] + [Bk.size[1] for Bk in B_blocks])
        indptr = numpy.concatenate([csr[0][bool(shift):]+shift for csr, shift in zip(csr_block, ishift[:-1])])
        indices = numpy.concatenate([csr[1]+shift for csr, shift in zip(csr_block, jshift[:-1])])
        data = numpy.concatenate([csr[2] for csr in csr_block])
        result = PETSc.Mat().createAIJ((nrows, ncols), csr=(indptr, indices, data), comm=PETSc.COMM_SELF)
        for B in B_blocks:
            B.destroy()
    return result


def diff_matrix(ndim, formdegree, A00, A11, A10):
    if formdegree == ndim:
        ncols = A10.shape[0]**ndim
        A_zero = PETSc.Mat().createAIJ((1, ncols), nnz=(0, 0), comm=PETSc.COMM_SELF)
        A_zero.assemble()
        return A_zero

    A00 = petsc_sparse(A00)
    A11 = petsc_sparse(A11)
    A10 = petsc_sparse(A10)
    if ndim == 1:
        return A10
    elif ndim == 2:
        if formdegree == 0:
            A_blocks = [[A00.kron(A10)], [A10.kron(A00)]]
        elif formdegree == 1:
            A_blocks = [[A10.kron(A11), A11.kron(A10)]]
            A_blocks[-1][-1].scale(-1)
    elif ndim == 3:
        if formdegree == 0:
            A_blocks = [[kron3(A00, A00, A10)], [kron3(A00, A10, A00)], [kron3(A10, A00, A00)]]
        elif formdegree == 1:
            size = tuple(A11.getSize()[k] * A10.getSize()[k] * A00.getSize()[k] for k in range(2))
            A_zero = PETSc.Mat().createAIJ(size, nnz=(0, 0), comm=PETSc.COMM_SELF)
            A_zero.assemble()
            A_blocks = [[kron3(A00, A10, A11, scale=-1), kron3(A00, A11, A10), A_zero],
                        [kron3(A10, A00, A11, scale=-1), A_zero, kron3(A11, A00, A10)],
                        [A_zero, kron3(A10, A11, A00), kron3(A11, A10, A00, scale=-1)]]
        elif formdegree == 2:
            A_blocks = [[kron3(A10, A11, A11, scale=-1), kron3(A11, A10, A11), kron3(A11, A11, A10)]]

    A00.destroy()
    A11.destroy()
    A10.destroy()
    result = block_mat(A_blocks)
    for A_row in A_blocks:
        for A in A_row:
            A.destroy()
    return result


def assemble_reference_tensor(A, Ahat, Vrow, Vcol, rmap, cmap, addv=None):
    if addv is None:
        addv = PETSc.InsertMode.INSERT

    update_A = FDMPC.load_set_values()
    rlocal, nel = glonum_fun(Vrow.cell_node_map(), bsize=Vrow.value_size)
    clocal, nel = glonum_fun(Vcol.cell_node_map(), bsize=Vcol.value_size)

    def cell_to_global(lgmap, cell_to_local, e, result=None):
        result = cell_to_local(e, result=result)
        return lgmap.apply(result, result=result)

    rindices = None
    cindices = None
    for e in range(nel):
        rindices = cell_to_global(rmap, rlocal, e, result=rindices)
        cindices = cell_to_global(cmap, clocal, e, result=cindices)
        update_A(A, Ahat, rindices, cindices, addv)
    A.assemble()


def diff_prolongator(Vf, Vc, fbcs=[], cbcs=[]):
    from tsfc.finatinterface import create_element
    from firedrake.preconditioners.pmg import fiat_reference_prolongator

    ef = Vf.finat_element
    ec = Vc.finat_element
    if ef.formdegree - ec.formdegree != 1:
        raise ValueError("Expecting Vf = d(Vc)")

    elements = list(set(get_base_elements(ec) + get_base_elements(ef)))
    elements = sorted(elements, key=lambda e: e.formdegree)
    e0, e1 = elements[::len(elements)-1]

    degree = e0.degree()
    A11 = numpy.eye(degree, dtype=PETSc.RealType)
    A00 = numpy.eye(degree+1, dtype=PETSc.RealType)
    A10 = fiat_reference_prolongator(e1, e0, derivative=True)

    ndim = Vc.mesh().topological_dimension()
    Dhat = diff_matrix(ndim, ec.formdegree, A00, A11, A10)

    scalar_element = lambda e: e._sub_element if isinstance(e, (ufl.TensorElement, ufl.VectorElement)) else e
    fdofs = restricted_dofs(ef, create_element(unrestrict_element(scalar_element(Vf.ufl_element()))))
    cdofs = restricted_dofs(ec, create_element(unrestrict_element(scalar_element(Vc.ufl_element()))))
    fises = PETSc.IS().createGeneral(fdofs, comm=PETSc.COMM_SELF)
    cises = PETSc.IS().createGeneral(cdofs, comm=PETSc.COMM_SELF)
    temp = Dhat
    Dhat = temp.createSubMatrix(fises, cises)
    fises.destroy()
    cises.destroy()
    temp.destroy()
    if Vf.value_size > 1:
        temp = Dhat
        eye = petsc_sparse(numpy.eye(Vf.value_size, dtype=PETSc.RealType))
        Dhat = temp.kron(eye)
        temp.destroy()
        eye.destroy()

    fmap = Vf.local_to_global_map(fbcs)
    cmap = Vc.local_to_global_map(cbcs)
    imode = PETSc.InsertMode.INSERT
    sizes = tuple(V.dof_dset.layout_vec.getSizes() for V in (Vf, Vc))
    block_size = Vf.dof_dset.layout_vec.getBlockSize()
    prealloc = PETSc.Mat().create(comm=Vf.comm)
    prealloc.setType(PETSc.Mat.Type.PREALLOCATOR)
    prealloc.setSizes(sizes)
    prealloc.setUp()
    assemble_reference_tensor(prealloc, Dhat, Vf, Vc, fmap, cmap, addv=imode)
    nnz = get_preallocation(prealloc, block_size * Vf.dof_dset.set.size)
    prealloc.destroy()
    Dmat = PETSc.Mat().createAIJ(sizes, block_size, nnz=nnz, comm=Vf.comm)
    Dmat.setOption(PETSc.Mat.Option.NEW_NONZERO_ALLOCATION_ERR, True)
    assemble_reference_tensor(Dmat, Dhat, Vf, Vc, fmap, cmap, addv=imode)
    Dhat.destroy()
    return Dmat


def unrestrict_element(ele):
    if isinstance(ele, ufl.VectorElement):
        return type(ele)(unrestrict_element(ele._sub_element), dim=ele.num_sub_elements())
    elif isinstance(ele, ufl.TensorElement):
        return type(ele)(unrestrict_element(ele._sub_element), shape=ele._shape, symmetry=ele.symmetry())
    elif isinstance(ele, ufl.EnrichedElement):
        return type(ele)(*list(dict.fromkeys(unrestrict_element(e) for e in ele._elements)))
    elif isinstance(ele, ufl.TensorProductElement):
        return type(ele)(*(unrestrict_element(e) for e in ele.sub_elements()), cell=ele.cell())
    elif isinstance(ele, ufl.MixedElement):
        return type(ele)(*(unrestrict_element(e) for e in ele.sub_elements()))
    elif isinstance(ele, ufl.WithMapping):
        return type(ele)(unrestrict_element(ele.wrapee), ele.mapping())
    elif isinstance(ele, ufl.RestrictedElement):
        return unrestrict_element(ele._element)
    elif isinstance(ele, (ufl.HDivElement, ufl.HCurlElement, ufl.BrokenElement)):
        return type(ele)(unrestrict_element(ele._element))
    else:
        return ele


def get_base_elements(e):
    import finat
    import FIAT
    if isinstance(e, finat.EnrichedElement):
        return sum(list(map(get_base_elements, e.elements)), [])
    elif isinstance(e, finat.TensorProductElement):
        return sum(list(map(get_base_elements, e.factors)), [])
    elif isinstance(e, finat.cube.FlattenedDimensions):
        return get_base_elements(e.product)
    elif isinstance(e, (finat.HCurlElement, finat.HDivElement)):
        return get_base_elements(e.wrappee)
    elif isinstance(e, finat.finiteelementbase.FiniteElementBase):
        return get_base_elements(e.fiat_equivalent)
    elif isinstance(e, FIAT.RestrictedElement):
        return get_base_elements(e._element)
    return [e]


class PoissonFDMPC(FDMPC):
    """
    A preconditioner for tensor-product elements that changes the shape
    functions so that the H^1 Riesz map is sparse in the interior of a
    Cartesian cell, and assembles a global sparse matrix on which other
    preconditioners, such as `ASMStarPC`, can be applied.

    Here we assume that the volume integrals in the Jacobian can be expressed as:

    inner(grad(v), alpha(grad(u)))*dx + inner(v, beta(u))*dx

    where alpha and beta are linear functions (tensor contractions).
    The sparse matrix is obtained by approximating alpha and beta by cell-wise
    constants and discarding the coefficients in alpha that couple together
    mixed derivatives and mixed components.

    For spaces that are not H^1-conforming, this preconditioner will use
    the symmetric interior-penalty DG method. The penalty coefficient can be
    provided in the application context, keyed on ``"eta"``.
    """

    _variant = "fdm_ipdg"

    def assemble_reference_tensor(self, V):
        from firedrake.preconditioners.pmg import get_line_elements
        try:
            line_elements, shifts = get_line_elements(V)
        except ValueError:
            raise ValueError("FDMPC does not support the element %s" % V.ufl_element())

        line_elements, = line_elements
        self.axes_shifts, = shifts

        degree = max(e.degree() for e in line_elements)
        eta = float(self.appctx.get("eta", degree*(degree+1)))
        element = V.finat_element
        is_dg = element.entity_dofs() == element.entity_closure_dofs()

        Afdm = []  # sparse interval mass and stiffness matrices for each direction
        Dfdm = []  # tabulation of normal derivatives at the boundary for each direction
        bdof = []  # indices of point evaluation dofs for each direction
        for e in line_elements:
            Afdm[:0], Dfdm[:0], bdof[:0] = tuple(zip(fdm_setup_ipdg(e, eta)))
            if not (e.formdegree or is_dg):
                Dfdm[0] = None
        return Afdm, Dfdm, bdof

    @PETSc.Log.EventDecorator("FDMSetValues")
    def set_values(self, A, Vrow, Vcol, addv, triu=False):
        """
        Assemble the stiffness matrix in the FDM basis using Kronecker products of interval matrices

        :arg A: the :class:`PETSc.Mat` to assemble
        :arg Vrow: the :class:`firedrake.FunctionSpace` test space
        :arg Vcol: the :class:`firedrake.FunctionSpace` trial space
        """
        set_values_csr = self.load_set_values(triu=triu)
        update_A = lambda A, Ae, rindices: set_values_csr(A, Ae, rindices, rindices, addv)
        condense_element_mat = self.get_static_condensation.get(Vrow, lambda x: x)
        condense_element_mat = lambda x: x

        get_rindices = self.cell_to_global[Vrow]
        rtensor = self.reference_tensor_on_diag.get(Vrow, None) or self.assemble_reference_tensor(Vrow)
        self.reference_tensor_on_diag[Vrow] = rtensor
        Afdm, Dfdm, bdof = rtensor

        Gq = self.coefficients.get("alpha")
        Bq = self.coefficients.get("beta")
        bcflags = self.coefficients.get("bcflags")
        Gq_facet = self.coefficients.get("Gq_facet")
        PT_facet = self.coefficients.get("PT_facet")

        V = Vrow
        bsize = V.value_size
        ncomp = V.ufl_element().reference_value_size()
        sdim = (V.finat_element.space_dimension() * bsize) // ncomp  # dimension of a single component
        ndim = V.ufl_domain().topological_dimension()
        shift = self.axes_shifts * bsize

        index_coef, _ = glonum_fun((Gq or Bq).cell_node_map())
        index_bc, _ = glonum_fun(bcflags.cell_node_map())
        flag2id = numpy.kron(numpy.eye(ndim, ndim, dtype=PETSc.IntType), [[1], [2]])

        # pshape is the shape of the DOFs in the tensor product
        pshape = tuple(Ak[0].size[0] for Ak in Afdm)
        static_condensation = False
        if sdim != numpy.prod(pshape):
            static_condensation = True

        if set(shift) != {0}:
            assert ncomp == ndim
            pshape = [tuple(numpy.roll(pshape, -shift[k])) for k in range(ncomp)]

        # assemble zero-th order term separately, including off-diagonals (mixed components)
        # I cannot do this for hdiv elements as off-diagonals are not sparse, this is because
        # the FDM eigenbases for GLL(N) and GLL(N-1) are not orthogonal to each other
        rindices = None
        use_diag_Bq = Bq is None or len(Bq.ufl_shape) != 2 or static_condensation
        if not use_diag_Bq:
            bshape = Bq.ufl_shape
            # Be = Bhat kron ... kron Bhat
            Be = Afdm[0][0].copy()
            for k in range(1, ndim):
                Be = Be.kron(Afdm[k][0])

            aptr = numpy.arange(0, (bshape[0]+1)*bshape[1], bshape[1], dtype=PETSc.IntType)
            aidx = numpy.tile(numpy.arange(bshape[1], dtype=PETSc.IntType), bshape[0])
            for e in range(self.nel):
                # Ae = Be kron Bq[e]
                adata = numpy.sum(Bq.dat.data_ro[index_coef(e)], axis=0)
                Ae = PETSc.Mat().createAIJWithArrays(bshape, (aptr, aidx, adata), comm=PETSc.COMM_SELF)
                Ae = Be.kron(Ae)
                rindices = get_rindices(e, result=rindices)
                update_A(A, Ae, rindices)
                Ae.destroy()
            Be.destroy()
            Bq = None

        # assemble the second order term and the zero-th order term if any,
        # discarding mixed derivatives and mixed componentsget_weak_bc_flags(J)
        mue = numpy.zeros((ncomp, ndim), dtype=PETSc.RealType)
        bqe = numpy.zeros((ncomp,), dtype=PETSc.RealType)

        for e in range(self.nel):
            je = index_coef(e)
            bce = bcflags.dat.data_ro_with_halos[index_bc(e)] > 1E-8

            rindices = get_rindices(e, result=rindices)
            rows = numpy.reshape(rindices, (-1, bsize))
            rows = numpy.transpose(rows)
            rows = numpy.reshape(rows, (ncomp, -1))

            # get second order coefficient on this cell
            if Gq is not None:
                mue.flat[:] = numpy.sum(Gq.dat.data_ro[je], axis=0)
            # get zero-th order coefficient on this cell
            if Bq is not None:
                bqe.flat[:] = numpy.sum(Bq.dat.data_ro[je], axis=0)

            for k in range(ncomp):
                # permutation of axes with respect to the first vector component
                axes = numpy.roll(numpy.arange(ndim), -shift[k])
                # for each component: compute the stiffness matrix Ae
                bck = bce[:, k] if len(bce.shape) == 2 else bce
                fbc = numpy.dot(bck, flag2id)

                if Gq is not None:
                    # Ae = mue[k][0] Ahat + bqe[k] Bhat
                    Be = Afdm[axes[0]][0].copy()
                    Ae = Afdm[axes[0]][1+fbc[0]].copy()
                    Ae.scale(mue[k][0])
                    if Bq is not None:
                        Ae.axpy(bqe[k], Be)

                    if ndim > 1:
                        # Ae = Ae kron Bhat + mue[k][1] Bhat kron Ahat
                        Ae = Ae.kron(Afdm[axes[1]][0])
                        if Gq is not None:
                            Ae.axpy(mue[k][1], Be.kron(Afdm[axes[1]][1+fbc[1]]))

                        if ndim > 2:
                            # Ae = Ae kron Bhat + mue[k][2] Bhat kron Bhat kron Ahat
                            Be = Be.kron(Afdm[axes[1]][0])
                            Ae = Ae.kron(Afdm[axes[2]][0])
                            if Gq is not None:
                                Ae.axpy(mue[k][2], Be.kron(Afdm[axes[2]][1+fbc[2]]))
                    Be.destroy()

                elif Bq is not None:
                    Ae = Afdm[axes[0]][0]
                    for m in range(1, ndim):
                        Ae = Ae.kron(Afdm[axes[m]][0])
                    Ae.scale(bqe[k])

                Ae = condense_element_mat(Ae)
                update_A(A, Ae, rows[k].astype(PETSc.IntType))
                Ae.destroy()

        # assemble SIPG interior facet terms if the normal derivatives have been set up
        if any(Dk is not None for Dk in Dfdm):
            if static_condensation:
                raise NotImplementedError("Static condensation for SIPG not implemented")
            if ndim < V.ufl_domain().geometric_dimension():
                raise NotImplementedError("SIPG on immersed meshes is not implemented")
            eta = float(self.appctx.get("eta"))

            lgmap = self.lgmaps[V]
            index_facet, local_facet_data, nfacets = get_interior_facet_maps(V)
            index_coef, _, _ = get_interior_facet_maps(Gq_facet or Gq)
            rows = numpy.zeros((2, sdim), dtype=PETSc.IntType)

            for e in range(nfacets):
                # for each interior facet: compute the SIPG stiffness matrix Ae
                ie = index_facet(e)
                je = numpy.reshape(index_coef(e), (2, -1))
                lfd = local_facet_data(e)
                idir = lfd // 2

                if PT_facet:
                    icell = numpy.reshape(lgmap.apply(ie), (2, ncomp, -1))
                    iord0 = numpy.insert(numpy.delete(numpy.arange(ndim), idir[0]), 0, idir[0])
                    iord1 = numpy.insert(numpy.delete(numpy.arange(ndim), idir[1]), 0, idir[1])
                    je = je[[0, 1], lfd]
                    Pfacet = PT_facet.dat.data_ro_with_halos[je]
                    Gfacet = Gq_facet.dat.data_ro_with_halos[je]
                else:
                    Gfacet = numpy.sum(Gq.dat.data_ro_with_halos[je], axis=1)

                for k in range(ncomp):
                    axes = numpy.roll(numpy.arange(ndim), -shift[k])
                    Dfacet = Dfdm[axes[0]]
                    if Dfacet is None:
                        continue

                    if PT_facet:
                        k0 = iord0[k] if shift != 1 else ndim-1-iord0[-k-1]
                        k1 = iord1[k] if shift != 1 else ndim-1-iord1[-k-1]
                        Piola = Pfacet[[0, 1], [k0, k1]]
                        mu = Gfacet[[0, 1], idir]
                    else:
                        if len(Gfacet.shape) == 3:
                            mu = Gfacet[[0, 1], [k, k], idir]
                        elif len(Gfacet.shape) == 2:
                            mu = Gfacet[[0, 1], idir]
                        else:
                            mu = Gfacet

                    offset = Dfacet.shape[0]
                    Adense = numpy.zeros((2*offset, 2*offset), dtype=PETSc.RealType)
                    dense_indices = []
                    for j, jface in enumerate(lfd):
                        j0 = j * offset
                        j1 = j0 + offset
                        jj = j0 + bdof[axes[0]][jface % 2]
                        dense_indices.append(jj)
                        for i, iface in enumerate(lfd):
                            i0 = i * offset
                            i1 = i0 + offset
                            ii = i0 + bdof[axes[0]][iface % 2]
                            sij = 0.5E0 if i == j else -0.5E0
                            if PT_facet:
                                smu = [sij*numpy.dot(numpy.dot(mu[0], Piola[i]), Piola[j]),
                                       sij*numpy.dot(numpy.dot(mu[1], Piola[i]), Piola[j])]
                            else:
                                smu = sij*mu

                            Adense[ii, jj] += eta * sum(smu)
                            Adense[i0:i1, jj] -= smu[i] * Dfacet[:, iface % 2]
                            Adense[ii, j0:j1] -= smu[j] * Dfacet[:, jface % 2]

                    Ae = numpy_to_petsc(Adense, dense_indices, diag=False)
                    if ndim > 1:
                        # assume that the mesh is oriented
                        Ae = Ae.kron(Afdm[axes[1]][0])
                        if ndim > 2:
                            Ae = Ae.kron(Afdm[axes[2]][0])

                    if bsize == ncomp:
                        icell = numpy.reshape(lgmap.apply(k+bsize*ie), (2, -1))
                        rows[0] = pull_axis(icell[0], pshape, idir[0])
                        rows[1] = pull_axis(icell[1], pshape, idir[1])
                    else:
                        assert pshape[k0][idir[0]] == pshape[k1][idir[1]]
                        rows[0] = pull_axis(icell[0][k0], pshape[k0], idir[0])
                        rows[1] = pull_axis(icell[1][k1], pshape[k1], idir[1])

                    update_A(A, Ae, rows)
                    Ae.destroy()

    def assemble_coef(self, J, form_compiler_parameters, discard_mixed=True, cell_average=True):
        from ufl import inner, diff
        from ufl.algorithms.ad import expand_derivatives

        coefficients = {}
        assembly_callables = []

        mesh = J.ufl_domain()
        tdim = mesh.topological_dimension()
        Finv = ufl.JacobianInverse(mesh)

        args_J = J.arguments()
        V = args_J[-1].function_space()
        degree = V.ufl_element().degree()
        try:
            degree = max(degree)
        except TypeError:
            pass
        quad_deg = 2*degree+1
        quad_deg = (form_compiler_parameters or {}).get("degree", quad_deg)
        dx = firedrake.dx(degree=quad_deg)

        if cell_average:
            family = "Discontinuous Lagrange" if tdim == 1 else "DQ"
            degree = 0
        else:
            family = "Quadrature"
            degree = quad_deg

        # extract coefficients directly from the bilinear form
        integrals_J = J.integrals_by_type("cell")
        mapping = args_J[0].ufl_element().mapping().lower()
        Piola = get_piola_tensor(mapping, mesh)

        # get second order coefficient
        ref_grad = [ufl.variable(ufl.grad(t)) for t in args_J]
        if Piola:
            replace_grad = {ufl.grad(t): ufl.dot(Piola, ufl.dot(dt, Finv)) for t, dt in zip(args_J, ref_grad)}
        else:
            replace_grad = {ufl.grad(t): ufl.dot(dt, Finv) for t, dt in zip(args_J, ref_grad)}

        alpha = expand_derivatives(sum([diff(diff(ufl.replace(i.integrand(), replace_grad),
                                             ref_grad[0]), ref_grad[1]) for i in integrals_J]))

        # get zero-th order coefficent
        ref_val = [ufl.variable(t) for t in args_J]
        if Piola:
            dummy_element = ufl.TensorElement("DQ", cell=mesh.ufl_cell(), degree=1, shape=Piola.ufl_shape)
            dummy_Piola = ufl.Coefficient(ufl.FunctionSpace(mesh, dummy_element))
            replace_val = {t: ufl.dot(dummy_Piola, s) for t, s in zip(args_J, ref_val)}
        else:
            replace_val = {t: s for t, s in zip(args_J, ref_val)}

        beta = expand_derivatives(sum([diff(diff(ufl.replace(i.integrand(), replace_val),
                                            ref_val[0]), ref_val[1]) for i in integrals_J]))
        if Piola:
            beta = ufl.replace(beta, {dummy_Piola: Piola})

        G = alpha
        if discard_mixed:
            # discard mixed derivatives and mixed components
            if len(G.ufl_shape) == 2:
                G = ufl.diag_vector(G)
            else:
                Gshape = G.ufl_shape
                Gshape = Gshape[:len(Gshape)//2]
                G = ufl.as_tensor(numpy.reshape([G[i+i] for i in numpy.ndindex(Gshape)], (Gshape[0], -1)))
            Qe = ufl.TensorElement(family, mesh.ufl_cell(), degree=degree, quad_scheme="default", shape=G.ufl_shape)
        else:
            Qe = ufl.TensorElement(family, mesh.ufl_cell(), degree=degree, quad_scheme="default", shape=G.ufl_shape, symmetry=True)

        # assemble second order coefficient
        if not isinstance(alpha, ufl.constantvalue.Zero):
            Q = firedrake.FunctionSpace(mesh, Qe)
            q = firedrake.TestFunction(Q)
            Gq = firedrake.Function(Q)
            coefficients["alpha"] = Gq
            assembly_callables.append(partial(firedrake.assemble, inner(G, q)*dx, Gq))

        # assemble zero-th order coefficient
        if not isinstance(beta, ufl.constantvalue.Zero):
            if Piola:
                # keep diagonal
                beta = ufl.diag_vector(beta)
            shape = beta.ufl_shape
            Qe = ufl.FiniteElement(family, mesh.ufl_cell(), degree=degree, quad_scheme="default")
            if shape:
                Qe = ufl.TensorElement(Qe, shape=shape)
            Q = firedrake.FunctionSpace(mesh, Qe)
            q = firedrake.TestFunction(Q)
            Bq = firedrake.Function(Q)
            coefficients["beta"] = Bq
            assembly_callables.append(partial(firedrake.assemble, inner(beta, q)*dx, Bq))

        if Piola:
            # make DGT functions with the second order coefficient
            # and the Piola tensor for each side of each facet
            extruded = mesh.cell_set._extruded
            dS_int = firedrake.dS_h(degree=quad_deg) + firedrake.dS_v(degree=quad_deg) if extruded else firedrake.dS(degree=quad_deg)
            ele = ufl.BrokenElement(ufl.FiniteElement("DGT", mesh.ufl_cell(), 0))
            area = ufl.FacetArea(mesh)

            replace_grad = {ufl.grad(t): ufl.dot(dt, Finv) for t, dt in zip(args_J, ref_grad)}
            alpha = expand_derivatives(sum([diff(diff(ufl.replace(i.integrand(), replace_grad),
                                                 ref_grad[0]), ref_grad[1]) for i in integrals_J]))
            vol = abs(ufl.JacobianDeterminant(mesh))
            G = vol * alpha
            G = ufl.as_tensor([[[G[i, k, j, k] for i in range(G.ufl_shape[0])] for j in range(G.ufl_shape[2])] for k in range(G.ufl_shape[3])])

            Q = firedrake.TensorFunctionSpace(mesh, ele, shape=G.ufl_shape)
            q = firedrake.TestFunction(Q)
            Gq_facet = firedrake.Function(Q)
            coefficients["Gq_facet"] = Gq_facet
            assembly_callables.append(partial(firedrake.assemble, ((inner(q('+'), G('+')) + inner(q('-'), G('-')))/area)*dS_int, Gq_facet))

            PT = Piola.T
            Q = firedrake.TensorFunctionSpace(mesh, ele, shape=PT.ufl_shape)
            q = firedrake.TestFunction(Q)
            PT_facet = firedrake.Function(Q)
            coefficients["PT_facet"] = PT_facet
            assembly_callables.append(partial(firedrake.assemble, ((inner(q('+'), PT('+')) + inner(q('-'), PT('-')))/area)*dS_int, PT_facet))

        # make DGT functions with BC flags
        rvs = V.ufl_element().reference_value_shape()
        cell = mesh.ufl_cell()
        family = "CG" if cell.topological_dimension() == 1 else "DGT"
        degree = 1 if cell.topological_dimension() == 1 else 0
        Qe = ufl.FiniteElement(family, cell=cell, degree=degree)
        if rvs:
            Qe = ufl.TensorElement(Qe, shape=rvs)
        Q = firedrake.FunctionSpace(mesh, Qe)
        q = firedrake.TestFunction(Q)
        bcflags = firedrake.Function(Q)

        ref_args = [ufl.variable(t) for t in args_J]
        replace_args = {t: s for t, s in zip(args_J, ref_args)}

        forms = []
        md = {"quadrature_degree": 0}
        for it in J.integrals():
            itype = it.integral_type()
            if itype.startswith("exterior_facet"):
                beta = ufl.diff(ufl.diff(ufl.replace(it.integrand(), replace_args), ref_args[0]), ref_args[1])
                beta = expand_derivatives(beta)
                if rvs:
                    beta = ufl.diag_vector(beta)
                ds_ext = ufl.Measure(itype, domain=mesh, subdomain_id=it.subdomain_id(), metadata=md)
                forms.append(ufl.inner(q, beta)*ds_ext)

        if len(forms):
            form = sum(forms)
            if len(form.arguments()) == 1:
                assembly_callables.append(partial(firedrake.assemble, form, bcflags))
                coefficients["bcflags"] = bcflags

        # set arbitrary non-zero coefficients for preallocation
        for coef in coefficients.values():
            with coef.dat.vec as cvec:
                cvec.set(1.0E0)
        self.coefficients = coefficients
        return coefficients, assembly_callables


def get_piola_tensor(mapping, domain):
    tdim = domain.topological_dimension()
    if mapping == 'identity':
        return None
    elif mapping == 'covariant piola':
        return ufl.JacobianInverse(domain).T * ufl.as_tensor(numpy.flipud(numpy.identity(tdim)))
    elif mapping == 'contravariant piola':
        sign = ufl.diag(ufl.as_tensor([-1]+[1]*(tdim-1)))
        return ufl.Jacobian(domain)*sign/ufl.JacobianDeterminant(domain)
    else:
        raise NotImplementedError("Unsupported element mapping %s" % mapping)


def pull_axis(x, pshape, idir):
    """permute x by reshaping into pshape and moving axis idir to the front"""
    return numpy.reshape(numpy.moveaxis(numpy.reshape(x.copy(), pshape), idir, 0), x.shape)


def set_submat_csr(A_global, A_local, global_indices, imode):
    """insert values from A_local to A_global on the diagonal block with indices global_indices"""
    indptr, indices, data = A_local.getValuesCSR()
    for i, row in enumerate(global_indices.flat):
        i0 = indptr[i]
        i1 = indptr[i+1]
        A_global.setValues(row, global_indices.flat[indices[i0:i1]], data[i0:i1], imode)


def numpy_to_petsc(A_numpy, dense_indices, diag=True, block=False):
    """
    Create a SeqAIJ Mat from a dense matrix using the diagonal and a subset of rows and columns.
    If dense_indices is empty, then also include the off-diagonal corners of the matrix.
    """
    n = A_numpy.shape[0]
    nbase = int(diag) if block else min(n, int(diag) + len(dense_indices))
    nnz = numpy.full((n,), nbase, dtype=PETSc.IntType)
    nnz[dense_indices] = len(dense_indices) if block else n

    imode = PETSc.InsertMode.INSERT
    A_petsc = PETSc.Mat().createAIJ(A_numpy.shape, nnz=(nnz, 0), comm=PETSc.COMM_SELF)

    idx = numpy.arange(n, dtype=PETSc.IntType)
    if block:
        values = A_numpy[dense_indices, :][:, dense_indices]
        A_petsc.setValues(dense_indices, dense_indices, values, imode)
    else:
        for j in dense_indices:
            A_petsc.setValues(j, idx, A_numpy[j, :], imode)
            A_petsc.setValues(idx, j, A_numpy[:, j], imode)

    if diag:
        idx = idx[:, None]
        values = A_numpy.diagonal()[:, None]
        A_petsc.setValuesRCV(idx, idx, values, imode)

    A_petsc.assemble()
    return A_petsc


@lru_cache(maxsize=10)
def fdm_setup_ipdg(fdm_element, eta):
    """
    Setup for the fast diagonalization method for the IP-DG formulation.
    Compute sparsified interval stiffness and mass matrices
    and tabulate the normal derivative of the shape functions.

    :arg fdm_element: a :class:`FIAT.FDMElement`
    :arg eta: penalty coefficient as a `float`

    :returns: 3-tuple of:
        Afdm: a list of :class:`PETSc.Mats` with the sparse interval matrices
        Bhat, and bcs(Ahat) for every combination of either natural or weak
        Dirichlet BCs on each endpoint.
        Dfdm: the tabulation of the normal derivatives of the Dirichlet eigenfunctions.
        bdof: the indices of PointEvaluation dofs.
    """
    from FIAT.quadrature import GaussLegendreQuadratureLineRule
    from FIAT.functional import PointEvaluation
    ref_el = fdm_element.get_reference_element()
    degree = fdm_element.degree()
    rule = GaussLegendreQuadratureLineRule(ref_el, degree+1)
    bdof = [k for k, f in enumerate(fdm_element.dual_basis()) if isinstance(f, PointEvaluation)]

    phi = fdm_element.tabulate(1, rule.get_points())
    Jhat = phi[(0, )]
    Dhat = phi[(1, )]
    Ahat = numpy.dot(numpy.multiply(Dhat, rule.get_weights()), Dhat.T)
    Bhat = numpy.dot(numpy.multiply(Jhat, rule.get_weights()), Jhat.T)

    # Facet normal derivatives
    basis = fdm_element.tabulate(1, ref_el.get_vertices())
    Dfacet = basis[(1,)]
    Dfacet[:, 0] = -Dfacet[:, 0]

    Afdm = [numpy_to_petsc(Bhat, bdof, block=True)]
    for bc in range(4):
        bcs = (bc % 2, bc//2)
        Abc = Ahat.copy()
        for k in range(2):
            if bcs[k] == 1:
                j = bdof[k]
                Abc[:, j] -= Dfacet[:, k]
                Abc[j, :] -= Dfacet[:, k]
                Abc[j, j] += eta
        Afdm.append(numpy_to_petsc(Abc, bdof))
    return Afdm, Dfacet, bdof


@lru_cache(maxsize=10)
def get_interior_facet_maps(V):
    """
    Extrude V.interior_facet_node_map and V.ufl_domain().interior_facets.local_facet_dat

    :arg V: a :class:`FunctionSpace`

    :returns: the 3-tuple of
        facet_to_nodes_fun: maps interior facets to the nodes of the two cells sharing it,
        local_facet_data_fun: maps interior facets to the local facet numbering in the two cells sharing it,
        nfacets: the total number of interior facets owned by this process
    """
    mesh = V.ufl_domain()
    intfacets = mesh.interior_facets
    facet_to_cells = intfacets.facet_cell_map.values
    local_facet_data = intfacets.local_facet_dat.data_ro

    facet_node_map = V.interior_facet_node_map()
    facet_to_nodes = facet_node_map.values
    nbase = facet_to_nodes.shape[0]

    if mesh.cell_set._extruded:
        facet_offset = facet_node_map.offset
        local_facet_data_h = numpy.array([5, 4], local_facet_data.dtype)

        cell_node_map = V.cell_node_map()
        cell_to_nodes = cell_node_map.values_with_halo
        cell_offset = cell_node_map.offset

        nelv = cell_node_map.values.shape[0]
        layers = facet_node_map.iterset.layers_array
        itype = cell_offset.dtype
        shift_h = numpy.array([[0], [1]], itype)

        if mesh.variable_layers:
            nv = 0
            to_base = []
            to_layer = []
            for f, cells in enumerate(facet_to_cells):
                istart = max(layers[cells, 0])
                iend = min(layers[cells, 1])
                nz = iend-istart-1
                nv += nz
                to_base.append(numpy.full((nz,), f, itype))
                to_layer.append(numpy.arange(nz, dtype=itype))

            nh = layers[:, 1]-layers[:, 0]-2
            to_base.append(numpy.repeat(numpy.arange(len(nh), dtype=itype), nh))
            to_layer += [numpy.arange(nf, dtype=itype) for nf in nh]

            to_base = numpy.concatenate(to_base)
            to_layer = numpy.concatenate(to_layer)
            nfacets = nv + sum(nh[:nelv])

            local_facet_data_fun = lambda e: local_facet_data[to_base[e]] if e < nv else local_facet_data_h
            facet_to_nodes_fun = lambda e: facet_to_nodes[to_base[e]] + to_layer[e]*facet_offset if e < nv else numpy.reshape(cell_to_nodes[to_base[e]] + numpy.kron(to_layer[e]+shift_h, cell_offset), (-1,))
        else:
            nelz = layers[0, 1]-layers[0, 0]-1
            nv = nbase * nelz
            nh = nelv * (nelz-1)
            nfacets = nv + nh

            local_facet_data_fun = lambda e: local_facet_data[e//nelz] if e < nv else local_facet_data_h
            facet_to_nodes_fun = lambda e: facet_to_nodes[e//nelz] + (e % nelz)*facet_offset if e < nv else numpy.reshape(cell_to_nodes[(e-nv)//(nelz-1)] + numpy.kron(((e-nv) % (nelz-1))+shift_h, cell_offset), (-1,))
    else:
        facet_to_nodes_fun = lambda e: facet_to_nodes[e]
        local_facet_data_fun = lambda e: local_facet_data[e]
        nfacets = nbase

    return facet_to_nodes_fun, local_facet_data_fun, nfacets


@lru_cache(maxsize=20)
def glonum_fun(node_map, bsize=1):
    """
    Return a the local numbering given an non-extruded local map and the total number of entities.

    :arg node_map: a :class:`pyop2.Map` mapping entities to their local dofs, including ghost entities.

    :returns: a 2-tuple with the map and the number of entities owned by this process
    """
    nelv = node_map.values.shape[0]
    if node_map.offset is None:
        nel = nelv

        def glonum(e, result=None):
            if result is None:
                result = numpy.copy(node_map.values_with_halo[e])
            else:
                numpy.copyto(result, node_map.values_with_halo[e])
            return result

    else:
        layers = node_map.iterset.layers_array
        if layers.shape[0] == 1:
            nelz = layers[0, 1]-layers[0, 0]-1
            nel = nelz*nelv

            def _glonum(node_map, nelz, e, result=None):
                if result is None:
                    result = numpy.copy(node_map.values_with_halo[e // nelz])
                else:
                    numpy.copyto(result, node_map.values_with_halo[e // nelz])
                result += (e % nelz)*node_map.offset
                return result
            glonum = partial(_glonum, node_map, nelz)

        else:
            nelz = layers[:, 1]-layers[:, 0]-1
            nel = sum(nelz[:nelv])
            to_base = numpy.repeat(numpy.arange(node_map.values_with_halo.shape[0], dtype=node_map.offset.dtype), nelz)
            to_layer = numpy.concatenate([numpy.arange(nz, dtype=node_map.offset.dtype) for nz in nelz])

            def _glonum(node_map, to_base, to_layer, e, result=None):
                if result is None:
                    result = numpy.copy(node_map.values_with_halo[to_base[e]])
                else:
                    numpy.copyto(result, node_map.values_with_halo[to_base[e]])
                result += to_layer[e]*node_map.offset
                return result
            glonum = partial(_glonum, node_map, to_base, to_layer)

    if bsize == 1:
        return glonum, nel

    ibase = numpy.arange(bsize, dtype=node_map.values.dtype)

    def vector_glonum(bsize, ibase, e, result=None):
        index = None
        if result is not None:
            index = result[:, 0]
        index = glonum(e, result=index)
        index *= bsize
        return numpy.add.outer(index, ibase, out=result)

    return partial(vector_glonum, bsize, ibase), nel


def glonum(node_map):
    """
    Return an array with the node map.

    :arg node_map: a :class:`pyop2.Map` mapping entities to their nodes, including ghost entities.

    :returns: a :class:`numpy.ndarray` whose rows are the nodes for each cell
    """
    if (node_map.offset is None) or (node_map.values_with_halo.size == 0):
        return node_map.values_with_halo
    else:
        layers = node_map.iterset.layers_array
        if layers.shape[0] == 1:
            nelz = layers[0, 1]-layers[0, 0]-1
            to_layer = numpy.tile(numpy.arange(nelz, dtype=node_map.offset.dtype), len(node_map.values_with_halo))
        else:
            nelz = layers[:, 1]-layers[:, 0]-1
            to_layer = numpy.concatenate([numpy.arange(nz, dtype=node_map.offset.dtype) for nz in nelz])
        return numpy.repeat(node_map.values_with_halo, nelz, axis=0) + numpy.kron(to_layer.reshape((-1, 1)), node_map.offset)


def spy(A, comm=None):
    import matplotlib.pyplot as plt
    import scipy.sparse as sp
    if comm is None:
        comm = A.comm
    nnz = A.getInfo()["nz_used"]
    if A.getType().endswith("sbaij"):
        A.setOption(PETSc.Mat.Option.GETROW_UPPERTRIANGULAR, True)
    csr = tuple(reversed(A.getValuesCSR()))
    if comm.rank == 0:
        csr[0].fill(1)
        scipy_mat = sp.csr_matrix(csr, shape=A.getSize())
        fig, axes = plt.subplots(nrows=1, ncols=1)
        axes.spy(scipy_mat, marker=".", markersize=2)
        plt.title("nnz(A) = %d" % nnz)
        plt.show()
