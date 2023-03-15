from functools import partial, lru_cache
from itertools import chain
from pyop2 import op2, PermutedMap
from firedrake.preconditioners.base import PCBase, SNESBase, PCSNESBase
from firedrake.dmhooks import (attach_hooks, get_appctx, push_appctx, pop_appctx,
                               add_hook, get_parent, push_parent, pop_parent,
                               get_function_space, set_function_space)
from firedrake.solving_utils import _SNESContext
from firedrake.tsfc_interface import extract_numbered_coefficients
from firedrake.utils import ScalarType_c, IntType_c, cached_property
from firedrake.petsc import PETSc
import firedrake
import finat
import ufl
import loopy
import numpy
import os
import tempfile

__all__ = ("PMGPC", "PMGSNES")


class PMGBase(PCSNESBase):
    """
    A class for implementing p-multigrid
    Internally, this sets up a DM with a custom coarsen routine
    that p-coarsens the problem. This DM is passed to an internal
    PETSc PC of type MG and with options prefix ``pmg_``. The
    relaxation to apply on every p-level is described by ``pmg_mg_levels_``,
    and the coarse solve by ``pmg_mg_coarse_``. Geometric multigrid
    or any other solver in firedrake may be applied to the coarse problem.

    Other PETSc options inspected by this class are:
    - 'pmg_mg_coarse_degree': polynomial degree of the coarse level
    - 'pmg_mg_coarse_mat_type': can be either 'aij' or 'matfree'
    - 'pmg_mg_coarse_form_compiler_mode': can be 'spectral' (default), 'vanilla', 'coffee', or 'tensor'
    - 'pmg_mg_levels_transfer_mat_type': can be either 'aij' or 'matfree'

    The p-coarsening is implemented in the `coarsen_element` routine.
    This takes in a :class:`ufl.FiniteElement` and either returns a
    new, coarser element, or raises a `ValueError` (if the supplied element
    should be the coarsest one of the hierarchy).

    The default coarsen_element is to perform power-of-2 reduction
    of the polynomial degree.
    It is expected that some applications of this preconditioner
    will subclass :class:`PMGBase` to override `coarsen_element` and
    `coarsen_form`.
    """

    _prefix = "pmg_"

    def coarsen_element(self, ele):
        """
        Coarsen a given element to form the next problem down in the p-hierarchy.

        If the supplied element should form the coarsest level of the p-hierarchy,
        raise `ValueError`. Otherwise, return a new :class:`ufl.FiniteElement`.

        By default, this does power-of-2 coarsening in polynomial degree until
        we reach the coarse degree specified through PETSc options (1 by default).

        :arg ele: a :class:`ufl.FiniteElement` to coarsen.
        """
        degree = PMGBase.max_degree(ele)
        if degree <= self.coarse_degree:
            raise ValueError
        return PMGBase.reconstruct_degree(ele, max(degree//2, self.coarse_degree))

    def coarsen_form(self, form, fine_to_coarse_map):
        """
        Coarsen a form, by replacing the solution, test and trial functions.
        """
        return ufl.replace(form, fine_to_coarse_map)

    def initialize(self, obj):
        # Make a new DM.
        # Hook up a (new) coarsen routine on that DM.
        # Make a new PC, of type MG.
        # Assign the DM to that PC.

        odm = obj.getDM()
        ctx = get_appctx(odm)
        if ctx is None:
            raise ValueError("No context found.")
        if not isinstance(ctx, _SNESContext):
            raise ValueError("Don't know how to get form from %r" % ctx)

        test, trial = ctx.J.arguments()
        if test.function_space() != trial.function_space():
            raise NotImplementedError("test and trial spaces must be the same")

        prefix = obj.getOptionsPrefix()
        options_prefix = prefix + self._prefix
        pdm = PETSc.DMShell().create(comm=obj.comm)
        pdm.setOptionsPrefix(options_prefix)

        ppc = self.configure_pmg(obj, pdm)
        is_snes = isinstance(obj, PETSc.SNES)

        copts = PETSc.Options(ppc.getOptionsPrefix() + ppc.getType() + "_coarse_")

        # Get the coarse degree from PETSc options
        fcp = ctx._problem.form_compiler_parameters
        mode = fcp.get("mode", "spectral") if fcp is not None else "spectral"
        self.coarse_degree = copts.getInt("degree", default=1)
        self.coarse_mat_type = copts.getString("mat_type", default=ctx.mat_type)
        self.coarse_pmat_type = copts.getString("pmat_type", default=self.coarse_mat_type)
        self.coarse_form_compiler_mode = copts.getString("form_compiler_mode", default=mode)

        # Construct a list with the elements we'll be using
        V = test.function_space()
        ele = V.ufl_element()
        elements = [ele]
        while True:
            try:
                ele_ = self.coarsen_element(ele)
                assert ele_.value_shape() == ele.value_shape()
                ele = ele_
            except ValueError:
                break
            elements.append(ele)

        sf = odm.getPointSF()
        section = odm.getDefaultSection()
        attach_hooks(pdm, level=len(elements)-1, sf=sf, section=section)
        # Now overwrite some routines on the DM
        pdm.setRefine(None)
        pdm.setCoarsen(self.coarsen)
        if is_snes:
            pdm.setSNESFunction(_SNESContext.form_function)
            pdm.setSNESJacobian(_SNESContext.form_jacobian)
            pdm.setKSPComputeOperators(_SNESContext.compute_operators)

        set_function_space(pdm, get_function_space(odm))

        parent = get_parent(odm)
        assert parent is not None
        add_hook(parent, setup=partial(push_parent, pdm, parent), teardown=partial(pop_parent, pdm, parent), call_setup=True)
        add_hook(parent, setup=partial(push_appctx, pdm, ctx), teardown=partial(pop_appctx, pdm, ctx), call_setup=True)

        ppc.incrementTabLevel(1, parent=obj)
        ppc.setFromOptions()
        ppc.setUp()
        self.ppc = ppc

    def update(self, obj):
        self.ppc.setUp()

    def view(self, obj, viewer=None):
        if viewer is None:
            viewer = PETSc.Viewer.STDOUT
        viewer.printfASCII("p-multigrid PC\n")
        if hasattr(self, "ppc"):
            self.ppc.view(viewer=viewer)

    def destroy(self, obj):
        if hasattr(self, "ppc"):
            self.ppc.destroy()

    def coarsen(self, fdm, comm):
        # Coarsen the _SNESContext of a DM fdm
        # return the coarse DM cdm of the coarse _SNESContext
        from firedrake.nullspace import VectorSpaceBasis, MixedVectorSpaceBasis

        fctx = get_appctx(fdm)
        parent = get_parent(fdm)
        assert parent is not None

        test, trial = fctx.J.arguments()
        fV = test.function_space()
        cele = self.coarsen_element(fV.ufl_element())

        # Have we already done this?
        cctx = fctx._coarse
        if cctx is not None:
            cV = cctx.J.arguments()[0].function_space()
            if (cV.ufl_element() == cele) and (cV.mesh() == fV.mesh()):
                return cV.dm

        cV = firedrake.FunctionSpace(fV.mesh(), cele)
        cdm = cV.dm

        fproblem = fctx._problem
        fu = fproblem.u
        cu = firedrake.Function(cV)

        fdeg = PMGBase.max_degree(fV.ufl_element())
        cdeg = PMGBase.max_degree(cV.ufl_element())

        fine_to_coarse_map = {fu: cu,
                              test: test.reconstruct(function_space=cV),
                              trial: trial.reconstruct(function_space=cV)}

        def _coarsen_form(a):
            if isinstance(a, ufl.Form):
                a = self.coarsen_form(a, fine_to_coarse_map)
                a = type(a)([f.reconstruct(metadata=self.coarsen_quadrature(f.metadata(), fdeg, cdeg))
                             for f in a.integrals()])
            return a

        cF = _coarsen_form(fctx.F)
        cJ = _coarsen_form(fctx.J)
        cJp = _coarsen_form(fctx.Jp)
        fcp = self.coarsen_quadrature(fproblem.form_compiler_parameters, fdeg, cdeg)
        cbcs = self.coarsen_bcs(fproblem.bcs, cV)
        cF = self.coarsen_residual(cF, cJ, cu)

        # Coarsen the appctx: the user might want to provide solution-dependant expressions and forms
        cappctx = dict(fctx.appctx)
        for key in cappctx:
            val = cappctx[key]
            if isinstance(val, dict):
                cappctx[key] = self.coarsen_quadrature(val, fdeg, cdeg)
            elif isinstance(val, ufl.Form):
                cappctx[key] = _coarsen_form(val)
            elif isinstance(val, ufl.classes.Expr):
                cappctx[key] = ufl.replace(val, fine_to_coarse_map)

        # If we're the coarsest grid of the p-hierarchy, don't
        # overwrite the coarsen routine; this is so that you can
        # use geometric multigrid for the p-coarse problem
        try:
            self.coarsen_element(cele)
            cdm.setCoarsen(self.coarsen)
            mat_type = fctx.mat_type
            pmat_type = fctx.pmat_type
        except ValueError:
            mat_type = self.coarse_mat_type
            pmat_type = self.coarse_pmat_type
            if fcp is None:
                fcp = dict()
            elif fcp is fproblem.form_compiler_parameters:
                fcp = dict(fcp)
            fcp["mode"] = self.coarse_form_compiler_mode

        # Coarsen the problem and the _SNESContext
        cproblem = firedrake.NonlinearVariationalProblem(cF, cu, bcs=cbcs, J=cJ, Jp=cJp,
                                                         form_compiler_parameters=fcp,
                                                         is_linear=fproblem.is_linear)

        cctx = type(fctx)(cproblem, mat_type, pmat_type,
                          appctx=cappctx,
                          pre_jacobian_callback=fctx._pre_jacobian_callback,
                          pre_function_callback=fctx._pre_function_callback,
                          post_jacobian_callback=fctx._post_jacobian_callback,
                          post_function_callback=fctx._post_function_callback,
                          options_prefix=fctx.options_prefix,
                          transfer_manager=fctx.transfer_manager)

        # FIXME setting up the _fine attribute triggers gmg injection.
        # cctx._fine = fctx
        fctx._coarse = cctx

        add_hook(parent, setup=partial(push_parent, cdm, parent), teardown=partial(pop_parent, cdm, parent), call_setup=True)
        add_hook(parent, setup=partial(push_appctx, cdm, cctx), teardown=partial(pop_appctx, cdm, cctx), call_setup=True)

        cdm.setOptionsPrefix(fdm.getOptionsPrefix())
        cdm.setKSPComputeOperators(_SNESContext.compute_operators)
        cdm.setCreateInterpolation(self.create_interpolation)
        cdm.setCreateInjection(self.create_injection)

        interp_petscmat, _ = cdm.createInterpolation(fdm)
        inject_petscmat = cdm.createInjection(fdm)

        if cu in cJ.coefficients():
            # injection of the initial state
            def inject_state():
                with cu.dat.vec_wo as xc, fu.dat.vec_ro as xf:
                    inject_petscmat.mult(xf, xc)

            add_hook(parent, setup=inject_state, call_setup=True)

        # coarsen the nullspace basis
        def coarsen_nullspace(coarse_V, mat, fine_nullspace):
            if isinstance(fine_nullspace, MixedVectorSpaceBasis):
                if mat.type == 'python':
                    mat = mat.getPythonContext()
                submats = [mat.getNestSubMatrix(i, i) for i in range(len(coarse_V))]
                coarse_bases = []
                for fs, submat, basis in zip(coarse_V, submats, fine_nullspace._bases):
                    if isinstance(basis, VectorSpaceBasis):
                        coarse_bases.append(coarsen_nullspace(fs, submat, basis))
                    else:
                        coarse_bases.append(coarse_V.sub(basis.index))
                return MixedVectorSpaceBasis(coarse_V, coarse_bases)
            elif isinstance(fine_nullspace, VectorSpaceBasis):
                coarse_vecs = []
                for xf in fine_nullspace._petsc_vecs:
                    wc = firedrake.Function(coarse_V)
                    with wc.dat.vec_wo as xc:
                        if mat.getSize()[1] == xf.getSize():
                            mat.mult(xf, xc)
                        else:
                            mat.multTranspose(xf, xc)
                    coarse_vecs.append(wc)
                vsb = VectorSpaceBasis(coarse_vecs, constant=fine_nullspace._constant)
                vsb.orthonormalize()
                return vsb
            else:
                return fine_nullspace

        ises = cV._ises
        cctx._nullspace = coarsen_nullspace(cV, interp_petscmat, fctx._nullspace)
        cctx.set_nullspace(cctx._nullspace, ises, transpose=False, near=False)
        cctx._near_nullspace = coarsen_nullspace(cV, interp_petscmat, fctx._near_nullspace)
        cctx.set_nullspace(cctx._near_nullspace, ises, transpose=False, near=True)
        cctx._nullspace_T = coarsen_nullspace(cV, interp_petscmat, fctx._nullspace_T)
        cctx.set_nullspace(cctx._nullspace_T, ises, transpose=True, near=False)
        return cdm

    def coarsen_quadrature(self, metadata, fdeg, cdeg):
        if isinstance(metadata, dict):
            # Coarsen the quadrature degree in a dictionary
            # preserving the ratio of quadrature nodes to interpolation nodes (qdeg+1)//(fdeg+1)
            qdeg = metadata.get("quadrature_degree", None)
            if qdeg is not None:
                cmd = dict(metadata)
                cmd["quadrature_degree"] = max(2*cdeg+1, ((qdeg+1)*(cdeg+1)+fdeg)//(fdeg+1)-1)
                return cmd
        return metadata

    def coarsen_bcs(self, fbcs, cV):
        cbcs = []
        for bc in fbcs:
            cV_ = cV
            for index in bc._indices:
                cV_ = cV_.sub(index)
            cbc_value = self.coarsen_bc_value(bc, cV_)
            if isinstance(bc, firedrake.DirichletBC):
                cbcs.append(bc.reconstruct(V=cV_, g=cbc_value))
            else:
                raise NotImplementedError("Unsupported BC type, please get in touch if you need this")
        return cbcs

    @staticmethod
    @lru_cache(maxsize=20)
    def create_transfer(cctx, fctx, mat_type, cbcs, fbcs):
        cbcs = cctx._problem.bcs if cbcs else []
        fbcs = fctx._problem.bcs if fbcs else []
        cV = cctx.J.arguments()[0].function_space()
        fV = fctx.J.arguments()[0].function_space()
        if mat_type == "matfree":
            return prolongation_matrix_matfree(fV, cV, fbcs, cbcs)
        elif mat_type == "aij":
            return prolongation_matrix_aij(fV, cV, fbcs, cbcs)
        else:
            raise ValueError("Unknown matrix type")

    def create_interpolation(self, dmc, dmf):
        prefix = dmc.getOptionsPrefix()
        mat_type = PETSc.Options(prefix).getString("mg_levels_transfer_mat_type", default="matfree")
        interpolate = self.create_transfer(get_appctx(dmc), get_appctx(dmf), mat_type, True, False)
        rscale = interpolate.createVecRight()  # only used as a workaround in the creation of coarse vecs
        return interpolate, rscale

    def create_injection(self, dmc, dmf):
        prefix = dmc.getOptionsPrefix()
        mat_type = PETSc.Options(prefix).getString("mg_levels_transfer_mat_type", default="matfree")
        return self.create_transfer(get_appctx(dmf), get_appctx(dmc), mat_type, False, False)

    @staticmethod
    def max_degree(ele):
        """
        Return the maximum degree of a :class:`ufl.FiniteElement`
        """
        if isinstance(ele, (ufl.VectorElement, ufl.TensorElement)):
            return PMGBase.max_degree(ele._sub_element)
        elif isinstance(ele, (ufl.MixedElement, ufl.TensorProductElement)):
            return max(PMGBase.max_degree(sub) for sub in ele.sub_elements())
        elif isinstance(ele, ufl.EnrichedElement):
            return max(PMGBase.max_degree(sub) for sub in ele._elements)
        elif isinstance(ele, ufl.WithMapping):
            return PMGBase.max_degree(ele.wrapee)
        elif isinstance(ele, (ufl.HDivElement, ufl.HCurlElement, ufl.BrokenElement, ufl.RestrictedElement)):
            return PMGBase.max_degree(ele._element)
        else:
            degree = ele.degree()
            try:
                return max(degree)
            except TypeError:
                return degree

    @staticmethod
    def reconstruct_degree(ele, degree):
        """
        Reconstruct an element, modifying its polynomial degree.

        By default, reconstructed EnrichedElements, TensorProductElements,
        and MixedElements will have the degree of the sub-elements shifted
        by the same amount so that the maximum degree is `degree`.
        This is useful to coarsen spaces like NCF(k) x DQ(k-1).

        :arg ele: a :class:`ufl.FiniteElement` to reconstruct,
        :arg degree: an integer degree.

        :returns: the reconstructed element
        """
        if isinstance(ele, ufl.VectorElement):
            return type(ele)(PMGBase.reconstruct_degree(ele._sub_element, degree), dim=ele.num_sub_elements())
        elif isinstance(ele, ufl.TensorElement):
            return type(ele)(PMGBase.reconstruct_degree(ele._sub_element, degree), shape=ele._shape, symmetry=ele.symmetry())
        elif isinstance(ele, ufl.EnrichedElement):
            shift = degree - PMGBase.max_degree(ele)
            return type(ele)(*(PMGBase.reconstruct_degree(e, PMGBase.max_degree(e) + shift) for e in ele._elements))
        elif isinstance(ele, ufl.TensorProductElement):
            shift = degree - PMGBase.max_degree(ele)
            return type(ele)(*(PMGBase.reconstruct_degree(e, PMGBase.max_degree(e) + shift) for e in ele.sub_elements()), cell=ele.cell())
        elif isinstance(ele, ufl.MixedElement):
            shift = degree - PMGBase.max_degree(ele)
            return type(ele)(*(PMGBase.reconstruct_degree(e, PMGBase.max_degree(e) + shift) for e in ele.sub_elements()))
        elif isinstance(ele, ufl.WithMapping):
            return type(ele)(PMGBase.reconstruct_degree(ele.wrapee, degree), ele.mapping())
        elif isinstance(ele, (ufl.HDivElement, ufl.HCurlElement, ufl.BrokenElement)):
            return type(ele)(PMGBase.reconstruct_degree(ele._element, degree))
        elif isinstance(ele, ufl.RestrictedElement):
            return type(ele)(PMGBase.reconstruct_degree(ele._element, degree), restriction_domain=ele._restriction_domain)
        else:
            return ele.reconstruct(degree=degree)


class PMGPC(PCBase, PMGBase):
    _prefix = "pmg_"

    def configure_pmg(self, pc, pdm):
        odm = pc.getDM()
        ppc = PETSc.PC().create(comm=pc.comm)
        ppc.setOptionsPrefix(pc.getOptionsPrefix() + "pmg_")
        ppc.setType("mg")
        ppc.setOperators(*pc.getOperators())
        ppc.setDM(pdm)

        # PETSc unfortunately requires us to make an ugly hack.
        # We would like to use GMG for the coarse solve, at least
        # sometimes. But PETSc will use this p-DM's getRefineLevels()
        # instead of the getRefineLevels() of the MeshHierarchy to
        # decide how many levels it should use for PCMG applied to
        # the p-MG's coarse problem. So we need to set an option
        # for the user, if they haven't already; I don't know any
        # other way to get PETSc to know this at the right time.
        opts = PETSc.Options(pc.getOptionsPrefix() + "pmg_")
        opts["mg_coarse_pc_mg_levels"] = odm.getRefineLevel() + 1

        return ppc

    def apply(self, pc, x, y):
        return self.ppc.apply(x, y)

    def applyTranspose(self, pc, x, y):
        return self.ppc.applyTranspose(x, y)

    def coarsen_bc_value(self, bc, cV):
        return 0

    def coarsen_residual(self, Fc, Jc, uc):
        return ufl.action(Jc, uc)


class PMGSNES(SNESBase, PMGBase):
    _prefix = "pfas_"

    def configure_pmg(self, snes, pdm):
        odm = snes.getDM()
        psnes = PETSc.SNES().create(comm=snes.comm)
        psnes.setOptionsPrefix(snes.getOptionsPrefix() + "pfas_")
        psnes.setType("fas")
        psnes.setDM(pdm)
        psnes.setTolerances(max_it=1)
        psnes.setConvergenceTest("skip")

        (f, residual) = snes.getFunction()
        assert residual is not None
        (fun, args, kargs) = residual
        psnes.setFunction(fun, f.duplicate(), args=args, kargs=kargs)

        pdm.setGlobalVector(f.duplicate())
        psnes.setSolution(snes.getSolution())

        # PETSc unfortunately requires us to make an ugly hack.
        # We would like to use GMG for the coarse solve, at least
        # sometimes. But PETSc will use this p-DM's getRefineLevels()
        # instead of the getRefineLevels() of the MeshHierarchy to
        # decide how many levels it should use for PCMG applied to
        # the p-MG's coarse problem. So we need to set an option
        # for the user, if they haven't already; I don't know any
        # other way to get PETSc to know this at the right time.
        opts = PETSc.Options(snes.getOptionsPrefix() + "pfas_")
        opts["fas_coarse_pc_mg_levels"] = odm.getRefineLevel() + 1
        opts["fas_coarse_snes_fas_levels"] = odm.getRefineLevel() + 1

        return psnes

    def step(self, snes, x, f, y):
        ctx = get_appctx(snes.dm)
        push_appctx(self.ppc.dm, ctx)
        x.copy(y)
        self.ppc.solve(snes.vec_rhs or None, y)
        y.aypx(-1, x)
        snes.setConvergedReason(self.ppc.getConvergedReason())
        pop_appctx(self.ppc.dm)

    def coarsen_bc_value(self, bc, cV):
        if not isinstance(bc._original_arg, firedrake.Function):
            return bc._original_arg

        coarse = firedrake.Function(cV)
        coarse.interpolate(bc._original_arg)
        return coarse

    def coarsen_residual(self, Fc, Jc, uc):
        return Fc


def prolongation_transfer_kernel_action(Vf, expr):
    from tsfc import compile_expression_dual_evaluation
    from tsfc.finatinterface import create_element
    to_element = create_element(Vf.ufl_element())
    kernel = compile_expression_dual_evaluation(expr, to_element, Vf.ufl_element(), log=PETSc.Log.isActive())
    coefficients = extract_numbered_coefficients(expr, kernel.coefficient_numbers)
    if kernel.needs_external_coords:
        coefficients = [Vf.mesh().coordinates] + coefficients

    return op2.Kernel(kernel.ast, kernel.name,
                      requires_zeroed_output_arguments=True,
                      flop_count=kernel.flop_count,
                      events=(kernel.event,)), coefficients


def expand_element(ele):
    """
    Expand a FiniteElement as an EnrichedElement of TensorProductElements, discarding modifiers.
    """
    if isinstance(ele, finat.FlattenedDimensions):
        return expand_element(ele.product)
    elif isinstance(ele, (finat.HDivElement, finat.HCurlElement)):
        return expand_element(ele.wrappee)
    elif isinstance(ele, finat.DiscontinuousElement):
        return expand_element(ele.element)
    elif isinstance(ele, finat.EnrichedElement):
        terms = list(map(expand_element, ele.elements))
        return finat.EnrichedElement(terms)
    elif isinstance(ele, finat.TensorProductElement):
        factors = list(map(expand_element, ele.factors))
        terms = [tuple()]
        for e in factors:
            new_terms = []
            for f in e.elements if isinstance(e, finat.EnrichedElement) else [e]:
                f_factors = tuple(f.factors) if isinstance(f, finat.TensorProductElement) else (f,)
                new_terms.extend([t_factors + f_factors for t_factors in terms])
            terms = new_terms
        terms = list(map(finat.TensorProductElement, terms))
        return finat.EnrichedElement(terms)
    else:
        return ele


def evaluate_dual(dual, element, key=None):
    keys = set(tuple(phi.get_point_dict().keys()) for phi in dual)
    pts = list(set(sum(keys, ())))
    if key is None:
        key = (0, ) * len(pts[0])
    tab = element.tabulate(sum(key), pts)[key]
    result = numpy.empty((len(dual), element.space_dimension()), dtype=tab.dtype)
    zero = [(0.0, ())]
    for k, phi in enumerate(dual):
        wts = phi.get_point_dict()
        wts = numpy.array([wts.get(pt, zero)[0][0] for pt in pts])
        result[k] = tab.dot(wts).T
    return result


def compare_element(e1, e2):
    if e1 is e2:
        return True
    if e1.space_dimension() != e2.space_dimension():
        return False
    B = evaluate_dual(e1.dual_basis(), e2)
    numpy.fill_diagonal(B, numpy.diagonal(B)-1.0)
    return numpy.allclose(B, 0.0, rtol=1E-14, atol=1E-14)


def compare_dual(b1, b2):
    p1 = b1.get_point_dict()
    p2 = b2.get_point_dict()
    if len(p1) != len(p2):
        return False

    k1 = numpy.array(list(p1.keys()))
    k2 = numpy.array(list(p2.keys()))
    if not numpy.allclose(k1, k2, rtol=1E-16, atol=1E-16):
        return False

    k1 = numpy.array([p1[k][0][0] for k in p1])
    k2 = numpy.array([p2[k][0][0] for k in p2])
    if not numpy.allclose(k1, k2, rtol=1E-16, atol=1E-16):
        return False
    return True


def compare_dual_basis(l1, l2):
    if len(l1) != len(l2):
        return False
    for b1, b2 in zip(l1, l2):
        if not compare_dual(b1, b2):
            return False
    return True


@lru_cache(maxsize=10)
@PETSc.Log.EventDecorator("GetLineElements")
def get_permutation_to_line_elements(finat_element):
    from FIAT.reference_element import LINE

    expansion = expand_element(finat_element)
    if expansion.space_dimension() != finat_element.space_dimension():
        raise ValueError("Failed to decompose %s into tensor products" % finat_element)

    line_elements = []
    terms = expansion.elements if hasattr(expansion, "elements") else [expansion]
    for term in terms:
        factors = term.factors if hasattr(term, "factors") else (term,)
        fiat_factors = tuple(e.fiat_equivalent for e in reversed(factors))
        if not all([e.get_reference_element().shape == LINE for e in fiat_factors]):
            raise ValueError("Failed to decompose %s into line elements" % fiat_factors)
        line_elements.append(fiat_factors)

    shapes = [tuple(e.space_dimension() for e in factors) for factors in line_elements]
    sizes = list(map(numpy.prod, shapes))
    dof_ranges = numpy.cumsum([0] + sizes)

    dof_perm = []
    unique_line_elements = []
    shifts = []

    visit = [False for e in line_elements]
    while False in visit:
        base = line_elements[visit.index(False)]
        tdim = len(base)
        pshape = tuple(e.space_dimension() for e in base)
        unique_line_elements.append(base)

        axes_shifts = tuple()
        for shift in range(tdim):
            if finat_element.formdegree != 2:
                shift = (tdim - shift) % tdim

            perm = base[shift:] + base[:shift]
            for i, term in enumerate(line_elements):
                if not visit[i]:
                    is_perm = all([e1.space_dimension() == e2.space_dimension()
                                   for e1, e2 in zip(perm, term)])
                    for e1, e2 in zip(perm, term):
                        if is_perm:
                            is_perm = compare_element(e1, e2)

                    if is_perm:
                        axes_shifts += ((tdim - shift) % tdim, )
                        dofs = numpy.arange(*dof_ranges[i:i+2], dtype=PETSc.IntType).reshape(pshape)
                        dofs = numpy.transpose(dofs, axes=numpy.roll(numpy.arange(tdim), -shift))
                        assert dofs.shape == shapes[i]
                        dof_perm.append(dofs.flat)
                        visit[i] = True
                        break

        shifts.append(axes_shifts)

    dof_perm = numpy.concatenate(dof_perm)
    return dof_perm, unique_line_elements, shifts


@lru_cache(maxsize=10)
def fiat_reference_prolongator(felem, celem, derivative=False):
    from FIAT.reference_element import flatten_reference_cube

    ref_el = flatten_reference_cube(felem.get_reference_element())
    tdim = ref_el.get_spatial_dimension()
    if derivative and tdim > 1:
        raise NotImplementedError("Derivative prolongator is only available on the interval")
    ckey = (felem.formdegree,) if derivative else (0,)*tdim
    fkey = (celem.formdegree,) if derivative else (0,)*tdim

    fdual = felem.dual_basis()
    cdual = celem.dual_basis()
    if fkey == ckey and compare_dual_basis(fdual, cdual):
        return numpy.array([])
    return evaluate_dual(fdual, celem, ckey)


# Common kernel to compute y = kron(A3, kron(A2, A1)) * x
# Vector and tensor field generalization from Deville, Fischer, and Mund section 8.3.1.
kronmxv_code = """
#include <petscsys.h>
#include <petscblaslapack.h>

static inline void kronmxv_inplace(PetscBLASInt tflag,
    PetscBLASInt mx, PetscBLASInt my, PetscBLASInt mz,
    PetscBLASInt nx, PetscBLASInt ny, PetscBLASInt nz, PetscBLASInt nel,
    PetscScalar *A1, PetscScalar *A2, PetscScalar *A3,
    PetscScalar **x, PetscScalar **y){

/*
Kronecker matrix-vector product

y = op(A) * x,  A = kron(A3, kron(A2, A1))

where:
op(A) = transpose(A) if tflag>0 else A
op(A1) is mx-by-nx,
op(A2) is my-by-ny,
op(A3) is mz-by-nz,
x is (nx*ny*nz)-by-nel,
y is (mx*my*mz)-by-nel.

Important notes:
The input data in x is destroyed in the process.
Need to allocate nel*max(mx, nx)*max(my, ny)*max(mz, nz) memory for both x and y.
*/

PetscScalar *ptr[2] = {*x, *y};
PetscScalar zero = 0.0E0, one = 1.0E0;
PetscBLASInt m, n, k, s, p, lda;
PetscBLASInt ires = 0;

char tran = 'T', notr = 'N';
char TA1 = tflag ? tran : notr;
char TA2 = tflag ? notr : tran;

if(A1){
    m = mx; k = nx; n = ny*nz*nel;
    lda = tflag ? nx : mx;
    BLASgemm_(&TA1, &notr, &m, &n, &k, &one, A1, &lda, ptr[ires], &k, &zero, ptr[!ires], &m);
    ires = !ires;
}
if(A2){
    p = 0; s = 0;
    m = mx; k = ny; n = my;
    lda = tflag ? ny : my;
    for(PetscBLASInt i=0; i<nz*nel; i++){
        BLASgemm_(&notr, &TA2, &m, &n, &k, &one, ptr[ires]+p, &m, A2, &lda, &zero, ptr[!ires]+s, &m);
        p += m*k;
        s += m*n;
    }
    ires = !ires;
}
if(A3){
    p = 0; s = 0;
    m = mx*my; k = nz; n = mz;
    lda = tflag ? nz : mz;
    for(PetscBLASInt i=0; i<nel; i++){
        BLASgemm_(&notr, &TA2, &m, &n, &k, &one, ptr[ires]+p, &m, A3, &lda, &zero, ptr[!ires]+s, &m);
        p += m*k;
        s += m*n;
    }
    ires = !ires;
}
// Reassign pointers such that y always points to the result
*x = ptr[!ires];
*y = ptr[ires];
return;
}

static inline void kronmxv(PetscBLASInt tflag,
    PetscBLASInt mx, PetscBLASInt my, PetscBLASInt mz,
    PetscBLASInt nx, PetscBLASInt ny, PetscBLASInt nz, PetscBLASInt nel,
    PetscScalar *A1, PetscScalar *A2, PetscScalar *A3,
    PetscScalar *x, PetscScalar *y, PetscScalar *xwork, PetscScalar *ywork){

    PetscScalar *ptr[2] = {xwork, ywork};

    if(ptr[0] != x)
        for(PetscBLASInt j=0; j<nx*ny*nz*nel; j++)
            ptr[0][j] = x[j];

    kronmxv_inplace(tflag, mx, my, mz, nx, ny, nz, nel, A1, A2, A3, &ptr[0], &ptr[1]);

    if(ptr[1] != y)
        for(PetscBLASInt j=0; j<mx*my*mz*nel; j++)
            y[j] = ptr[1][j];
    return;
}

static inline void permute_axis(PetscBLASInt axis,
    PetscBLASInt n0, PetscBLASInt n1, PetscBLASInt n2, PetscBLASInt n3,
    PetscScalar *x, PetscScalar *y){

    PetscBLASInt p = 0;
    PetscBLASInt s0, s1, s2, s3;
    if (axis == 0){
        s0 = 1; s1 = s0*n0; s2 = s1*n1; s3 = s2*n2;
    }else if(axis == 1){
        s1 = 1; s2 = s1*n1; s0 = s2*n2; s3 = s0*n0;
    }else if(axis == 2){
        s2 = 1; s0 = s2*n2; s1 = s0*n0; s3 = s1*n1;
    }
    for(PetscBLASInt i3=0; i3<n3; i3++)
        for(PetscBLASInt i2=0; i2<n2; i2++)
            for(PetscBLASInt i1=0; i1<n1; i1++)
                for(PetscBLASInt i0=0; i0<n0; i0++)
                    y[s0*i0 + s1*i1 + s2*i2 + s3*i3] = x[p++];
    return;
}

static inline void ipermute_axis(PetscBLASInt axis,
    PetscBLASInt n0, PetscBLASInt n1, PetscBLASInt n2, PetscBLASInt n3,
    PetscScalar *x, PetscScalar *y){

    PetscBLASInt p = 0;
    PetscBLASInt s0, s1, s2, s3;
    if (axis == 0){
        s0 = 1; s1 = s0*n0; s2 = s1*n1; s3 = s2*n2;
    }else if(axis == 1){
        s1 = 1; s2 = s1*n1; s0 = s2*n2; s3 = s0*n0;
    }else if(axis == 2){
        s2 = 1; s0 = s2*n2; s1 = s0*n0; s3 = s1*n1;
    }

    for(PetscBLASInt i3=0; i3<n3; i3++)
        for(PetscBLASInt i2=0; i2<n2; i2++)
            for(PetscBLASInt i1=0; i1<n1; i1++)
                for(PetscBLASInt i0=0; i0<n0; i0++)
                    x[p++] += y[s0*i0 + s1*i1 + s2*i2 + s3*i3];
    return;
}
"""


@PETSc.Log.EventDecorator("MakeKronCode")
def make_kron_code(Vf, Vc, t_in, t_out, mat_name, scratch):
    """
    Return interpolation and restriction kernels between enriched tensor product elements
    """
    operator_decl = []
    prolong_code = []
    restrict_code = []
    _, felems, fshifts = get_permutation_to_line_elements(Vf.finat_element)
    _, celems, cshifts = get_permutation_to_line_elements(Vc.finat_element)

    shifts = fshifts
    in_place = False
    if len(felems) == len(celems):
        in_place = all([(len(fs)*Vf.value_size == len(cs)*Vc.value_size) for fs, cs in zip(fshifts, cshifts)])
        psize = Vf.value_size

    if not in_place:
        if len(celems) == 1:
            psize = Vc.value_size
            pelem = celems[0]
            perm_name = "perm_%s" % t_in
            celems = celems*len(felems)

        elif len(felems) == 1:
            shifts = cshifts
            psize = Vf.value_size
            pelem = felems[0]
            perm_name = "perm_%s" % t_out
            felems = felems*len(celems)
        else:
            raise ValueError("Cannot assign fine to coarse DOFs")

        if set(cshifts) == set(fshifts):
            csize = Vc.value_size * Vc.finat_element.space_dimension()
            prolong_code.append(f"""
            for({IntType_c} j=1; j<{len(fshifts)}; j++)
                for({IntType_c} i=0; i<{csize}; i++)
                    {t_in}[j*{csize} + i] = {t_in}[i];
            """)
            restrict_code.append(f"""
            for({IntType_c} j=1; j<{len(fshifts)}; j++)
                for({IntType_c} i=0; i<{csize}; i++)
                    {t_in}[i] += {t_in}[j*{csize} + i];
            """)

        elif pelem == celems[0]:
            for k in range(len(shifts)):
                if Vc.value_size*len(shifts[k]) < Vf.value_size:
                    shifts[k] = shifts[k]*(Vf.value_size//Vc.value_size)

            pshape = [e.space_dimension() for e in pelem]
            pargs = ", ".join(map(str, pshape+[1]*(3-len(pshape))))
            pstride = psize * numpy.prod(pshape)

            perm = sum(shifts, tuple())
            perm_data = ", ".join(map(str, perm))
            operator_decl.append(f"""
                PetscBLASInt {perm_name}[{len(perm)}] = {{ {perm_data} }};
            """)
            prolong_code.append(f"""
            for({IntType_c} j=1; j<{len(perm)}; j++)
                permute_axis({perm_name}[j], {pargs}, {psize}, {t_in}, {t_in}+j*{pstride});
            """)
            restrict_code.append(f"""
            for({IntType_c} j=1; j<{len(perm)}; j++)
                ipermute_axis({perm_name}[j], {pargs}, {psize}, {t_in}, {t_in}+j*{pstride});
            """)

    fskip = 0
    cskip = 0
    Jlen = 0
    Jmats = []
    fshapes = []
    cshapes = []
    has_code = False
    for felem, celem, shift in zip(felems, celems, shifts):
        if len(felem) != len(celem):
            raise ValueError("Fine and coarse elements do not have the same number of factors")
        if len(felem) > 3:
            raise ValueError("More than three factors are not supported")

        # Declare array shapes to be used as literals inside the kernels
        nscal = psize*len(shift)
        fshape = [e.space_dimension() for e in felem]
        cshape = [e.space_dimension() for e in celem]
        fshapes.append((nscal,) + tuple(fshape))
        cshapes.append((nscal,) + tuple(cshape))

        J = [fiat_reference_prolongator(fe, ce).T for fe, ce in zip(felem, celem)]
        if any([Jk.size and numpy.isclose(Jk, 0.0E0).all() for Jk in J]):
            prolong_code.append(f"""
            for({IntType_c} i=0; i<{nscal*numpy.prod(fshape)}; i++) {t_out}[i+{fskip}] = 0.0E0;
            """)
            restrict_code.append(f"""
            for({IntType_c} i=0; i<{nscal*numpy.prod(cshape)}; i++) {t_in}[i+{cskip}] = 0.0E0;
            """)
        else:
            Jsize = numpy.cumsum([Jlen]+[Jk.size for Jk in J])
            Jptrs = ["%s+%d" % (mat_name, Jsize[k]) if J[k].size else "NULL" for k in range(len(J))]
            Jmats.extend(J)
            Jlen = Jsize[-1]

            # The Kronecker product routines assume 3D shapes, so in 1D and 2D we pass NULL instead of J
            Jargs = ", ".join(Jptrs+["NULL"]*(3-len(Jptrs)))
            fargs = ", ".join(map(str, fshape+[1]*(3-len(fshape))))
            cargs = ", ".join(map(str, cshape+[1]*(3-len(cshape))))
            if in_place:
                prolong_code.append(f"""
            kronmxv_inplace(0, {fargs}, {cargs}, {nscal}, {Jargs}, &{t_in}, &{t_out});
                """)
                restrict_code.append(f"""
            kronmxv_inplace(1, {cargs}, {fargs}, {nscal}, {Jargs}, &{t_out}, &{t_in});
                """)
            elif shifts == fshifts:
                if has_code and psize > 1:
                    raise ValueError("Single tensor product to many tensor products not implemented for vectors")
                # Single tensor product to many
                prolong_code.append(f"""
            kronmxv(0, {fargs}, {cargs}, {nscal}, {Jargs}, {t_in}+{cskip}, {t_out}+{fskip}, {scratch}, {t_out}+{fskip});
                """)
                restrict_code.append(f"""
            kronmxv(1, {cargs}, {fargs}, {nscal}, {Jargs}, {t_out}+{fskip}, {t_in}+{cskip}, {t_out}+{fskip}, {scratch});
                """)
            else:
                # Many tensor products to single tensor product
                if has_code:
                    raise ValueError("Many tensor products to single tensor product not implemented")
                fskip = 0
                prolong_code.append(f"""
            kronmxv(0, {fargs}, {cargs}, {nscal}, {Jargs}, {t_in}+{cskip}, {t_out}+{fskip}, {t_in}+{cskip}, {t_out}+{fskip});
                """)
                restrict_code.append(f"""
            kronmxv(1, {cargs}, {fargs}, {nscal}, {Jargs}, {t_out}+{fskip}, {t_in}+{cskip}, {t_out}+{fskip}, {t_in}+{cskip});
                """)
            has_code = True
        fskip += nscal*numpy.prod(fshape)
        cskip += nscal*numpy.prod(cshape)

    # Pass the 1D interpolators as a hexadecimal string
    Jdata = ", ".join(map(float.hex, chain(*[Jk.flat for Jk in Jmats])))
    operator_decl.append(f"""
            PetscScalar {mat_name}[{Jlen}] = {{ {Jdata} }};
    """)

    operator_decl = "".join(operator_decl)
    prolong_code = "".join(prolong_code)
    restrict_code = "".join(reversed(restrict_code))
    shapes = [tuple(map(max, zip(*fshapes))), tuple(map(max, zip(*cshapes)))]

    if fskip > numpy.prod(shapes[0]):
        shapes[0] = (fskip, 1, 1, 1)
    if cskip > numpy.prod(shapes[1]):
        shapes[1] = (cskip, 1, 1, 1)
    return operator_decl, prolong_code, restrict_code, shapes


def get_piola_tensor(mapping, domain, inverse=False):
    mapping = mapping.lower()
    if mapping == "identity":
        return None
    elif mapping == "contravariant piola":
        if inverse:
            return ufl.JacobianInverse(domain)*ufl.JacobianDeterminant(domain)
        else:
            return ufl.Jacobian(domain)/ufl.JacobianDeterminant(domain)
    elif mapping == "covariant piola":
        if inverse:
            return ufl.Jacobian(domain).T
        else:
            return ufl.JacobianInverse(domain).T
    else:
        raise ValueError("Mapping %s is not supported" % mapping)


def cache_generate_code(kernel, comm):
    _cachedir = os.environ.get('PYOP2_CACHE_DIR',
                               os.path.join(tempfile.gettempdir(),
                                            'pyop2-cache-uid%d' % os.getuid()))

    key = kernel.cache_key[0]
    shard, disk_key = key[:2], key[2:]
    filepath = os.path.join(_cachedir, shard, disk_key)
    if os.path.exists(filepath):
        with open(filepath, 'r') as f:
            code = f.read()
    else:
        code = loopy.generate_code_v2(kernel.code).device_code()
        if comm.rank == 0:
            os.makedirs(os.path.join(_cachedir, shard), exist_ok=True)
            with open(filepath, 'w') as f:
                f.write(code)
        comm.barrier()
    return code


def make_mapping_code(Q, fmapping, cmapping, t_in, t_out):
    if fmapping == cmapping:
        return None
    A = get_piola_tensor(cmapping, Q.mesh(), inverse=False)
    B = get_piola_tensor(fmapping, Q.mesh(), inverse=True)
    tensor = A
    if B:
        tensor = ufl.dot(B, tensor) if tensor else B
    if tensor is None:
        tensor = ufl.Identity(Q.ufl_element().value_shape()[0])

    u = ufl.Coefficient(Q)
    expr = ufl.dot(tensor, u)
    prolong_map_kernel, coefficients = prolongation_transfer_kernel_action(Q, expr)
    prolong_map_code = cache_generate_code(prolong_map_kernel, Q._comm)
    prolong_map_code = prolong_map_code.replace("void expression_kernel", "static void prolongation_mapping")
    coefficients.remove(u)

    expr = ufl.dot(u, tensor)
    restrict_map_kernel, coefficients = prolongation_transfer_kernel_action(Q, expr)
    restrict_map_code = cache_generate_code(restrict_map_kernel, Q._comm)
    restrict_map_code = restrict_map_code.replace("void expression_kernel", "static void restriction_mapping")
    restrict_map_code = restrict_map_code.replace("#include <stdint.h>", "")
    restrict_map_code = restrict_map_code.replace("#include <complex.h>", "")
    coefficients.remove(u)

    coef_args = "".join([", c%d" % i for i in range(len(coefficients))])
    coef_decl = "".join([", PetscScalar const *restrict c%d" % i for i in range(len(coefficients))])
    qlen = Q.value_size * Q.finat_element.space_dimension()
    prolong_code = f"""
            for({IntType_c} i=0; i<{qlen}; i++) {t_out}[i] = 0.0E0;

            prolongation_mapping({t_out}{coef_args}, {t_in});
    """
    restrict_code = f"""
            for({IntType_c} i=0; i<{qlen}; i++) {t_in}[i] = 0.0E0;

            restriction_mapping({t_in}{coef_args}, {t_out});
    """
    mapping_code = prolong_map_code + restrict_map_code
    return coef_decl, prolong_code, restrict_code, mapping_code, coefficients


def make_permutation_code(V, vshape, pshape, t_in, t_out, array_name):
    _, _, shifts = get_permutation_to_line_elements(V.finat_element)
    shift = shifts[0]
    if shift != (0,):
        ndof = numpy.prod(vshape)
        permutation = numpy.reshape(numpy.arange(ndof), pshape)
        axes = numpy.arange(len(shift))
        for k in range(permutation.shape[0]):
            permutation[k] = numpy.reshape(numpy.transpose(permutation[k], axes=numpy.roll(axes, -shift[k])), pshape[1:])
        nflip = 0
        mapping = V.ufl_element().mapping().lower()
        if mapping == "contravariant piola":
            # flip the sign of the first component
            nflip = ndof//len(shift)
        elif mapping == "covariant piola":
            # flip the order of reference components
            permutation = numpy.flip(permutation, axis=0)

        permutation = numpy.transpose(numpy.reshape(permutation, vshape))
        pdata = ", ".join(map(str, permutation.flat))

        decl = f"""
            PetscInt {array_name}[{ndof}] = {{ {pdata} }};
        """
        prolong = f"""
            for({IntType_c} i=0; i<{ndof}; i++) {t_out}[{array_name}[i]] = {t_in}[i];
            for({IntType_c} i=0; i<{nflip}; i++) {t_out}[i] = -{t_out}[i];
        """
        restrict = f"""
            for({IntType_c} i=0; i<{nflip}; i++) {t_out}[i] = -{t_out}[i];
            for({IntType_c} i=0; i<{ndof}; i++) {t_in}[i] = {t_out}[{array_name}[i]];
        """
    else:
        decl = ""
        prolong = f"""
            for({IntType_c} j=0; j<{vshape[1]}; j++)
                for({IntType_c} i=0; i<{vshape[0]}; i++)
                    {t_out}[j + {vshape[1]}*i] = {t_in}[i + {vshape[0]}*j];
        """
        restrict = f"""
            for({IntType_c} j=0; j<{vshape[1]}; j++)
                for({IntType_c} i=0; i<{vshape[0]}; i++)
                    {t_in}[i + {vshape[0]}*j] = {t_out}[j + {vshape[1]}*i];
        """
    return decl, prolong, restrict


def get_permuted_map(V):
    """
    Return a PermutedMap with the same tensor product shape for
    every component of H(div) or H(curl) tensor product elements
    """
    indices, _, _ = get_permutation_to_line_elements(V.finat_element)
    if all(indices[:-1] < indices[1:]):
        return V.cell_node_map()
    return PermutedMap(V.cell_node_map(), indices)


class StandaloneInterpolationMatrix(object):
    """
    Interpolation matrix for a single standalone space.
    """

    _cache_work = {}

    def __init__(self, Vf, Vc, Vf_bcs, Vc_bcs):
        self.Vf_bcs = Vf_bcs
        self.Vc_bcs = Vc_bcs
        if isinstance(Vf, firedrake.Function):
            self.uf = Vf
            Vf = Vf.function_space()
        else:
            if Vf not in self._cache_work:
                self._cache_work[Vf] = firedrake.Function(Vf)
            self.uf = self._cache_work[Vf]
        if isinstance(Vc, firedrake.Function):
            self.uc = Vc
            Vc = Vc.function_space()
        else:
            if Vc not in self._cache_work:
                self._cache_work[Vc] = firedrake.Function(Vc)
            self.uc = self._cache_work[Vc]
        self.Vf = Vf
        self.Vc = Vc

    @cached_property
    def _weight(self):
        weight = firedrake.Function(self.Vf)
        size = self.Vf.finat_element.space_dimension() * self.Vf.value_size
        kernel_code = f"""
        void weight(PetscScalar *restrict w){{
            for(PetscInt i=0; i<{size}; i++) w[i] += 1.0;
            return;
        }}
        """
        kernel = op2.Kernel(kernel_code, "weight", requires_zeroed_output_arguments=True)
        op2.par_loop(kernel, weight.cell_set, weight.dat(op2.INC, weight.cell_node_map()))
        with weight.dat.vec as w:
            w.reciprocal()
        return weight

    @cached_property
    def _kernels(self):
        try:
            uf_map = get_permuted_map(self.Vf)
            uc_map = get_permuted_map(self.Vc)
            prolong_kernel, restrict_kernel, coefficients = self.make_blas_kernels(self.Vf, self.Vc)
            prolong_args = [prolong_kernel, self.uf.cell_set,
                            self.uf.dat(op2.INC, uf_map),
                            self.uc.dat(op2.READ, uc_map),
                            self._weight.dat(op2.READ, uf_map)]
        except ValueError:
            uf_map = self.Vf.cell_node_map()
            uc_map = self.Vc.cell_node_map()
            prolong_kernel, restrict_kernel, coefficients = self.make_kernels(self.Vf, self.Vc)
            prolong_args = [prolong_kernel, self.uf.cell_set,
                            self.uf.dat(op2.WRITE, uf_map),
                            self.uc.dat(op2.READ, uc_map)]

        restrict_args = [restrict_kernel, self.uf.cell_set,
                         self.uc.dat(op2.INC, uc_map),
                         self.uf.dat(op2.READ, uf_map),
                         self._weight.dat(op2.READ, uf_map)]
        coefficient_args = [c.dat(op2.READ, c.cell_node_map()) for c in coefficients]
        prolong = partial(op2.par_loop, *prolong_args, *coefficient_args)
        restrict = partial(op2.par_loop, *restrict_args, *coefficient_args)
        return prolong, restrict

    def _prolong(self):
        with self.uf.dat.vec_wo as uf:
            uf.set(0.0E0)
        self._kernels[0]()

    def _restrict(self):
        with self.uc.dat.vec_wo as uc:
            uc.set(0.0E0)
        self._kernels[1]()

    def view(self, mat, viewer=None):
        if viewer is None:
            return
        typ = viewer.getType()
        if typ != PETSc.Viewer.Type.ASCII:
            return
        viewer.printfASCII("Firedrake matrix-free prolongator %s\n" %
                           type(self).__name__)

    def getInfo(self, mat, info=None):
        from mpi4py import MPI
        memory = self.uf.dat.nbytes + self.uc.dat.nbytes
        if self._weight is not None:
            memory += self._weight.dat.nbytes
        if info is None:
            info = PETSc.Mat.InfoType.GLOBAL_SUM
        if info == PETSc.Mat.InfoType.LOCAL:
            return {"memory": memory}
        elif info == PETSc.Mat.InfoType.GLOBAL_SUM:
            gmem = mat.comm.tompi4py().allreduce(memory, op=MPI.SUM)
            return {"memory": gmem}
        elif info == PETSc.Mat.InfoType.GLOBAL_MAX:
            gmem = mat.comm.tompi4py().allreduce(memory, op=MPI.MAX)
            return {"memory": gmem}
        else:
            raise ValueError("Unknown info type %s" % info)

    @staticmethod
    def make_blas_kernels(Vf, Vc):
        """
        Interpolation and restriction kernels between CG / DG
        tensor product spaces on quads and hexes.

        Works by tabulating the coarse 1D basis functions
        as the (fdegree+1)-by-(cdegree+1) matrix Jhat,
        and using the fact that the 2D / 3D tabulation is the
        tensor product J = kron(Jhat, kron(Jhat, Jhat))
        """
        felem = Vf.ufl_element()
        celem = Vc.ufl_element()
        fmapping = felem.mapping().lower()
        cmapping = celem.mapping().lower()

        in_place_mapping = False
        coefficients = []
        mapping_code = ""
        coef_decl = ""

        if fmapping == cmapping:
            # interpolate on each direction via Kroncker product
            operator_decl, prolong_code, restrict_code, shapes = make_kron_code(Vf, Vc, "t0", "t1", "J0", "t2")
        else:
            decl = [""]*4
            prolong = [""]*5
            restrict = [""]*5
            # get embedding element for Vf with identity mapping and collocated vector component DOFs
            try:
                qelem = felem
                if qelem.mapping() != "identity":
                    qelem = qelem.reconstruct(mapping="identity")
                Qf = Vf if qelem == felem else firedrake.FunctionSpace(Vf.mesh(), qelem)
                mapping_output = make_mapping_code(Qf, fmapping, cmapping, "t0", "t1")
                in_place_mapping = True
            except Exception:
                qelem = ufl.FiniteElement("DQ", cell=felem.cell(), degree=PMGBase.max_degree(felem))
                if felem.value_shape():
                    qelem = ufl.TensorElement(qelem, shape=felem.value_shape(), symmetry=felem.symmetry())
                Qf = firedrake.FunctionSpace(Vf.mesh(), qelem)
                mapping_output = make_mapping_code(Qf, fmapping, cmapping, "t0", "t1")

            qshape = (Qf.value_size, Qf.finat_element.space_dimension())
            # interpolate to embedding fine space
            decl[0], prolong[0], restrict[0], shapes = make_kron_code(Qf, Vc, "t0", "t1", "J0", "t2")

            if mapping_output is not None:
                # permute to FInAT ordering, and apply the mapping
                decl[1], restrict[1], prolong[1] = make_permutation_code(Vc, qshape, shapes[0], "t0", "t1", "perm0")
                coef_decl, prolong[2], restrict[2], mapping_code, coefficients = mapping_output
                if not in_place_mapping:
                    # permute to Kronecker-friendly ordering and interpolate to fine space
                    decl[2], prolong[3], restrict[3] = make_permutation_code(Vf, qshape, shapes[0], "t1", "t0", "perm1")
                    decl[3], prolong[4], restrict[4], _shapes = make_kron_code(Vf, Qf, "t0", "t1", "J1", "t2")
                    shapes.extend(_shapes)

            operator_decl = "".join(decl)
            prolong_code = "".join(prolong)
            restrict_code = "".join(reversed(restrict))

        # FInAT elements order the component DOFs related to the same node contiguously.
        # We transpose before and after the multiplication times J to have each component
        # stored contiguously as a scalar field, thus reducing the number of dgemm calls.

        # We could benefit from loop tiling for the transpose, but that makes the code
        # more complicated.

        fshape = (Vf.value_size, Vf.finat_element.space_dimension())
        cshape = (Vc.value_size, Vc.finat_element.space_dimension())

        lwork = numpy.prod([max(*dims) for dims in zip(*shapes)])
        lwork = max(lwork, max(numpy.prod(fshape), numpy.prod(cshape)))

        if cshape[0] == 1:
            coarse_read = f"""for({IntType_c} i=0; i<{numpy.prod(cshape)}; i++) t0[i] = x[i];"""
            coarse_write = f"""for({IntType_c} i=0; i<{numpy.prod(cshape)}; i++) x[i] += t0[i];"""
        else:
            coarse_read = f"""
            for({IntType_c} j=0; j<{cshape[1]}; j++)
                for({IntType_c} i=0; i<{cshape[0]}; i++)
                    t0[j + {cshape[1]}*i] = x[i + {cshape[0]}*j];
            """
            coarse_write = f"""
            for({IntType_c} j=0; j<{cshape[1]}; j++)
                for({IntType_c} i=0; i<{cshape[0]}; i++)
                    x[i + {cshape[0]}*j] += t0[j + {cshape[1]}*i];
            """
        if (fshape[0] == 1) or in_place_mapping:
            fine_read = f"""for({IntType_c} i=0; i<{numpy.prod(fshape)}; i++) t1[i] = y[i] * w[i];"""
            fine_write = f"""for({IntType_c} i=0; i<{numpy.prod(fshape)}; i++) y[i] += t1[i] * w[i];"""
        else:
            fine_read = f"""
            for({IntType_c} j=0; j<{fshape[1]}; j++)
                for({IntType_c} i=0; i<{fshape[0]}; i++)
                    t1[j + {fshape[1]}*i] = y[i + {fshape[0]}*j] * w[i + {fshape[0]}*j];
            """
            fine_write = f"""
            for({IntType_c} j=0; j<{fshape[1]}; j++)
                for({IntType_c} i=0; i<{fshape[0]}; i++)
                   y[i + {fshape[0]}*j] += t1[j + {fshape[1]}*i] * w[i + {fshape[0]}*j];
            """
        kernel_code = f"""
        {mapping_code}

        {kronmxv_code}

        void prolongation(PetscScalar *restrict y, const PetscScalar *restrict x,
                          const PetscScalar *restrict w{coef_decl}){{
            PetscScalar work[3][{lwork}] = {{0.0E0}};
            PetscScalar *t0 = work[0];
            PetscScalar *t1 = work[1];
            PetscScalar *t2 = work[2];
            {operator_decl}
            {coarse_read}
            {prolong_code}
            {fine_write}
            return;
        }}

        void restriction(PetscScalar *restrict x, const PetscScalar *restrict y,
                         const PetscScalar *restrict w{coef_decl}){{
            PetscScalar work[3][{lwork}] = {{0.0E0}};
            PetscScalar *t0 = work[0];
            PetscScalar *t1 = work[1];
            PetscScalar *t2 = work[2];
            {operator_decl}
            {fine_read}
            {restrict_code}
            {coarse_write}
            return;
        }}
        """
        from firedrake.slate.slac.compiler import BLASLAPACK_LIB, BLASLAPACK_INCLUDE
        prolong_kernel = op2.Kernel(kernel_code, "prolongation", include_dirs=BLASLAPACK_INCLUDE.split(),
                                    ldargs=BLASLAPACK_LIB.split(), requires_zeroed_output_arguments=True)
        restrict_kernel = op2.Kernel(kernel_code, "restriction", include_dirs=BLASLAPACK_INCLUDE.split(),
                                     ldargs=BLASLAPACK_LIB.split(), requires_zeroed_output_arguments=True)
        return prolong_kernel, restrict_kernel, coefficients

    def make_kernels(self, Vf, Vc):
        """
        Interpolation and restriction kernels between arbitrary elements.

        This is temporary while we wait for dual evaluation in FInAT.
        """
        prolong_kernel, _ = prolongation_transfer_kernel_action(Vf, self.uc)
        matrix_kernel, coefficients = prolongation_transfer_kernel_action(Vf, firedrake.TestFunction(Vc))
        # The way we transpose the prolongation kernel is suboptimal.
        # A local matrix is generated each time the kernel is executed.
        element_kernel = loopy.generate_code_v2(matrix_kernel.code).device_code()
        element_kernel = element_kernel.replace("void expression_kernel", "static void expression_kernel")
        coef_args = "".join([", c%d" % i for i in range(len(coefficients))])
        coef_decl = "".join([", const %s *restrict c%d" % (ScalarType_c, i) for i in range(len(coefficients))])
        dimc = Vc.finat_element.space_dimension() * Vc.value_size
        dimf = Vf.finat_element.space_dimension() * Vf.value_size
        restrict_code = f"""
        {element_kernel}

        void restriction({ScalarType_c} *restrict Rc, const {ScalarType_c} *restrict Rf, const {ScalarType_c} *restrict w{coef_decl})
        {{
            {ScalarType_c} Afc[{dimf}*{dimc}] = {{0}};
            expression_kernel(Afc{coef_args});
            for ({IntType_c} i = 0; i < {dimf}; i++)
               for ({IntType_c} j = 0; j < {dimc}; j++)
                   Rc[j] += Afc[i*{dimc} + j] * Rf[i] * w[i];
        }}
        """
        restrict_kernel = op2.Kernel(restrict_code, "restriction", requires_zeroed_output_arguments=True)
        return prolong_kernel, restrict_kernel, coefficients

    def multTranspose(self, mat, rf, rc):
        """
        Implement restriction: restrict residual on fine grid rf to coarse grid rc.
        """
        with self.uf.dat.vec_wo as uf:
            rf.copy(uf)
        for bc in self.Vf_bcs:
            bc.zero(self.uf)

        self._restrict()

        for bc in self.Vc_bcs:
            bc.zero(self.uc)
        with self.uc.dat.vec_ro as uc:
            uc.copy(rc)

    def mult(self, mat, xc, xf, inc=False):
        """
        Implement prolongation: prolong correction on coarse grid xc to fine grid xf.
        """
        with self.uc.dat.vec_wo as uc:
            xc.copy(uc)
        for bc in self.Vc_bcs:
            bc.zero(self.uc)

        self._prolong()

        for bc in self.Vf_bcs:
            bc.zero(self.uf)
        if inc:
            with self.uf.dat.vec_ro as uf:
                xf.axpy(1.0, uf)
        else:
            with self.uf.dat.vec_ro as uf:
                uf.copy(xf)

    def multAdd(self, mat, x, y, w):
        if y.handle == w.handle:
            self.mult(mat, x, w, inc=True)
        else:
            self.mult(mat, x, w)
            w.axpy(1.0, y)


class MixedInterpolationMatrix(StandaloneInterpolationMatrix):
    """
    Interpolation matrix for a mixed finite element space.
    """
    @cached_property
    def _weight(self):
        return None

    @cached_property
    def _standalones(self):
        standalones = []
        for (i, (uf_sub, uc_sub)) in enumerate(zip(self.uf.subfunctions, self.uc.subfunctions)):
            Vf_sub_bcs = [bc for bc in self.Vf_bcs if bc.function_space().index == i]
            Vc_sub_bcs = [bc for bc in self.Vc_bcs if bc.function_space().index == i]
            standalone = StandaloneInterpolationMatrix(uf_sub, uc_sub, Vf_sub_bcs, Vc_sub_bcs)
            standalones.append(standalone)
        return standalones

    @cached_property
    def _kernels(self):
        prolong = lambda: [s._prolong() for s in self._standalones]
        restrict = lambda: [s._restrict() for s in self._standalones]
        return prolong, restrict

    def getNestSubMatrix(self, i, j):
        if i == j:
            s = self._standalones[i]
            sizes = (s.uf.dof_dset.layout_vec.getSizes(), s.uc.dof_dset.layout_vec.getSizes())
            M_shll = PETSc.Mat().createPython(sizes, s, comm=s.uf._comm)
            M_shll.setUp()
            return M_shll
        else:
            return None


def prolongation_matrix_aij(Pk, P1, Pk_bcs=[], P1_bcs=[]):
    if isinstance(Pk, firedrake.Function):
        Pk = Pk.function_space()
    if isinstance(P1, firedrake.Function):
        P1 = P1.function_space()
    sp = op2.Sparsity((Pk.dof_dset,
                       P1.dof_dset),
                      (Pk.cell_node_map(),
                       P1.cell_node_map()))
    mat = op2.Mat(sp, PETSc.ScalarType)
    mesh = Pk.mesh()

    fele = Pk.ufl_element()
    if isinstance(fele, ufl.MixedElement) and not isinstance(fele, (ufl.VectorElement, ufl.TensorElement)):
        for i in range(fele.num_sub_elements()):
            Pk_bcs_i = [bc for bc in Pk_bcs if bc.function_space().index == i]
            P1_bcs_i = [bc for bc in P1_bcs if bc.function_space().index == i]

            rlgmap, clgmap = mat[i, i].local_to_global_maps
            rlgmap = Pk.sub(i).local_to_global_map(Pk_bcs_i, lgmap=rlgmap)
            clgmap = P1.sub(i).local_to_global_map(P1_bcs_i, lgmap=clgmap)
            unroll = any(bc.function_space().component is not None
                         for bc in chain(Pk_bcs_i, P1_bcs_i) if bc is not None)
            matarg = mat[i, i](op2.WRITE, (Pk.sub(i).cell_node_map(), P1.sub(i).cell_node_map()),
                               lgmaps=((rlgmap, clgmap), ), unroll_map=unroll)
            expr = firedrake.TestFunction(P1.sub(i))
            kernel, coefficients = prolongation_transfer_kernel_action(Pk.sub(i), expr)
            parloop_args = [kernel, mesh.cell_set, matarg]
            for coefficient in coefficients:
                m_ = coefficient.cell_node_map()
                parloop_args.append(coefficient.dat(op2.READ, m_))

            op2.par_loop(*parloop_args)

    else:
        rlgmap, clgmap = mat.local_to_global_maps
        rlgmap = Pk.local_to_global_map(Pk_bcs, lgmap=rlgmap)
        clgmap = P1.local_to_global_map(P1_bcs, lgmap=clgmap)
        unroll = any(bc.function_space().component is not None
                     for bc in chain(Pk_bcs, P1_bcs) if bc is not None)
        matarg = mat(op2.WRITE, (Pk.cell_node_map(), P1.cell_node_map()),
                     lgmaps=((rlgmap, clgmap), ), unroll_map=unroll)
        expr = firedrake.TestFunction(P1)
        kernel, coefficients = prolongation_transfer_kernel_action(Pk, expr)
        parloop_args = [kernel, mesh.cell_set, matarg]
        for coefficient in coefficients:
            m_ = coefficient.cell_node_map()
            parloop_args.append(coefficient.dat(op2.READ, m_))

        op2.par_loop(*parloop_args)

    mat.assemble()
    return mat.handle


def prolongation_matrix_matfree(Vf, Vc, Vf_bcs=[], Vc_bcs=[]):
    fele = Vf.ufl_element()
    if isinstance(fele, ufl.MixedElement) and not isinstance(fele, (ufl.VectorElement, ufl.TensorElement)):
        ctx = MixedInterpolationMatrix(Vf, Vc, Vf_bcs, Vc_bcs)
    else:
        ctx = StandaloneInterpolationMatrix(Vf, Vc, Vf_bcs, Vc_bcs)

    sizes = (Vf.dof_dset.layout_vec.getSizes(), Vc.dof_dset.layout_vec.getSizes())
    M_shll = PETSc.Mat().createPython(sizes, ctx, comm=Vf._comm)
    M_shll.setUp()
    return M_shll
