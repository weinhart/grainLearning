# parameters: ranges
parameters = {
    'param': [0, 4],
    'param0': [0, 4],
    'param1': [0, 4],
}
# measurements: data, weight, solver
measurements = {
    'measure0': {'data': 1, 'weight': 1, 'solver': './TestCalibration -fit identity1', 'output': 'identity1'},
    'measure1': {'data': [2, 3], 'weight': [1, 1], 'solver': './TestCalibration -fit identity2',
                 'output': 'identity2'}
}
# number of iterations K (runs iterations 0 to K-1)
n_iterations = 5
# number of samples per iteration
n_samples = 30
# number of components in the Gaussian mixture model
n_gmm = 2
# minimum effective sample size
ess_min = 0.2
# maximum covariance
sigma_max = 1
# build directory
mercury_build = '/Users/weinhartt/Code/Lab/cmake-build-debug/Drivers/Calibration'
# output directory
output_dir = 'identity2'
# call the calibration
from calibrate import *
calibrate(parameters, measurements, n_iterations, n_samples, n_gmm, ess_min, sigma_max, mercury_build, output_dir,
          analysis=True)
