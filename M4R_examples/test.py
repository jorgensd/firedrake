from firedrake import *

# number of elements in each direction
n = 10

# create mesh
mesh = IntervalMesh(n, 0, pi)

# create function space
V = FunctionSpace(mesh, "CG", 1)

# Define the trial and test functions
u = TrialFunction(V)
v = TestFunction(V)

# Define the variational form
a = (inner(grad(u), grad(v))) * dx

# Apply the homogeneous Dirichlet boundary conditions
bc = DirichletBC(V, 0.0, "on_boundary")

# Create eigenproblem with boundary conditions
eigenprob = LinearEigenproblem(a, bcs=bc)

# Create corresponding eigensolver, looking for 1 eigenvalue
eigensolver = LinearEigensolver(eigenprob, 2)

# Solve the problem
ncov = eigensolver.solve()

vr, vi = eigensolver.eigenfunction(0)

print(type(vr), vi)

'''TESTING THE EVALS'''
# for i in range(ncov):
#     print(eigensolver.eigenvalue(i))
# # View eigenvalues and eigenvectors
# eval_1 = eigensolver.eigenvalue(0)
# vr, vi = eigensolver.eigenfunction(0)

# print('eval')
# print(eval_1)



# h = pi /5
# ans = 6 / h**2
# print(ans * 2)
# k = 1
# ans *= (1-cos(k*h))/(2+cos(k*h))
# print(ans)