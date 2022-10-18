from pyop2.mpi import MPI, internal_comm, decref
from itertools import zip_longest

__all__ = ("Ensemble", )


class Ensemble(object):
    def __init__(self, comm, M):
        """
        Create a set of space and ensemble subcommunicators.

        :arg comm: The communicator to split.
        :arg M: the size of the communicators used for spatial parallelism.
        :raises ValueError: if ``M`` does not divide ``comm.size`` exactly.
        """
        size = comm.size

        if (size // M)*M != size:
            raise ValueError("Invalid size of subcommunicators %d does not divide %d" % (M, size))

        rank = comm.rank

        # User global comm
        self.global_comm = comm
        # Internal global comm
        self._global_comm = internal_comm(comm)

        # User split comm
        self.comm = self.global_comm.Split(color=(rank // M), key=rank)
        # Internal split comm
        self._comm = internal_comm(self.comm)
        """The communicator for spatial parallelism, contains a
        contiguous chunk of M processes from :attr:`comm`"""

        # User ensemble comm
        self.ensemble_comm = self.global_comm.Split(color=(rank % M), key=rank)
        # Internal ensemble comm
        self._ensemble_comm = internal_comm(self.ensemble_comm)
        """The communicator for ensemble parallelism, contains all
        processes in :attr:`comm` which have the same rank in
        :attr:`comm`."""

        assert self.comm.size == M
        assert self.ensemble_comm.size == (size // M)

    def __del__(self):
        if hasattr(self, "comm"):
            self.comm.Free()
            del self.comm
        if hasattr(self, "ensemble_comm"):
            self.ensemble_comm.Free()
            del self.ensemble_comm
        for comm_name in ["_global_comm", "_comm", "_ensemble_comm"]:
            if hasattr(self, comm_name):
                comm = getattr(self, comm_name)
                decref(comm)

    def allreduce(self, f, f_reduced, op=MPI.SUM):
        """
        Allreduce a function f into f_reduced over :attr:`ensemble_comm`.

        :arg f: The a :class:`.Function` to allreduce.
        :arg f_reduced: the result of the reduction.
        :arg op: MPI reduction operator.
        :raises ValueError: if communicators mismatch, or function sizes mismatch.
        """
        if MPI.Comm.Compare(f_reduced._comm, f._comm) not in {MPI.CONGRUENT, MPI.IDENT}:
            raise ValueError("Mismatching communicators for functions")
        if MPI.Comm.Compare(f._comm, self._comm) not in {MPI.CONGRUENT, MPI.IDENT}:
            raise ValueError("Function communicator does not match space communicator")
        with f_reduced.dat.vec_wo as vout, f.dat.vec_ro as vin:
            if vout.getSizes() != vin.getSizes():
                raise ValueError("Mismatching sizes")
            self._ensemble_comm.Allreduce(vin.array_r, vout.array, op=op)
        return f_reduced

    def send(self, f, dest, tag=0):
        """
        Send (blocking) a function f over :attr:`ensemble_comm` to another
        ensemble rank.

        :arg f: The a :class:`.Function` to send
        :arg dest: the rank to send to
        :arg tag: the tag of the message
        """
        if MPI.Comm.Compare(f._comm, self._comm) not in {MPI.CONGRUENT, MPI.IDENT}:
            raise ValueError("Function communicator does not match space communicator")
        for dat in f.dat:
            self._ensemble_comm.Send(dat.data_ro, dest=dest, tag=tag)

    def recv(self, f, source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG, statuses=None):
        """
        Receive (blocking) a function f over :attr:`ensemble_comm` from
        another ensemble rank.

        :arg f: The a :class:`.Function` to receive into
        :arg source: the rank to receive from
        :arg tag: the tag of the message
        :arg statuses: MPI.Status objects (one for each of f.split() or None).
        """
        if MPI.Comm.Compare(f._comm, self._comm) not in {MPI.CONGRUENT, MPI.IDENT}:
            raise ValueError("Function communicator does not match space communicator")
        if statuses is not None and len(statuses) != len(f.dat):
            raise ValueError("Need to provide enough status objects for all parts of the Function")
        for dat, status in zip_longest(f.dat, statuses or (), fillvalue=None):
            self._ensemble_comm.Recv(dat.data, source=source, tag=tag, status=status)

    def isend(self, f, dest, tag=0):
        """
        Send (non-blocking) a function f over :attr:`ensemble_comm` to another
        ensemble rank.

        :arg f: The a :class:`.Function` to send
        :arg dest: the rank to send to
        :arg tag: the tag of the message
        :returns: list of MPI.Request objects (one for each of f.split()).
        """
        if MPI.Comm.Compare(f._comm, self._comm) not in {MPI.CONGRUENT, MPI.IDENT}:
            raise ValueError("Function communicator does not match space communicator")
        return [self._ensemble_comm.Isend(dat.data_ro, dest=dest, tag=tag)
                for dat in f.dat]

    def irecv(self, f, source=MPI.ANY_SOURCE, tag=MPI.ANY_TAG):
        """
        Receive (non-blocking) a function f over :attr:`ensemble_comm` from
        another ensemble rank.

        :arg f: The a :class:`.Function` to receive into
        :arg source: the rank to receive from
        :arg tag: the tag of the message
        :returns: list of MPI.Request objects (one for each of f.split()).
        """
        if MPI.Comm.Compare(f._comm, self._comm) not in {MPI.CONGRUENT, MPI.IDENT}:
            raise ValueError("Function communicator does not match space communicator")
        return [self._ensemble_comm.Irecv(dat.data, source=source, tag=tag)
                for dat in f.dat]
