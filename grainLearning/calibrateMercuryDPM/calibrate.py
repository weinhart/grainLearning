import argparse
import inspect
import itertools

from plotResults import *
from simulate import *
from smc import *

def calibrate(parameters, measurements, n_iterations=3, n_samples=0, n_gmm=2, ess_min=0.2, sigma_max=1,
              mercury_build='.', output_dir='', test=False, verbose=False, nodes=[], cores=0, analysis=False):
    """ calibrates based on given input parameters """

    # set default for n_samples
    if n_samples == 0:
        n_samples = 10 * len(parameters)
    # set default for output_file_name
    for k in measurements.keys():
        if not 'output' in measurements[k]:
            measurements[k]['output'] = measurements[k]['solver'].split()[0].split('/')[-1]
    # set default output_dir
    if not output_dir:
        output_dir = os.path.basename(inspect.stack()[-1][1]).removeprefix('calibrate_').removesuffix('.py')
    # check existence of mercury_build directory
    if not os.path.exists(os.path.expanduser(mercury_build)):
        raise ValueError("\n Build directory %s does not exist" % os.path.expanduser(mercury_build))


    # write out input parameters
    print("Calibrate based on \n"
          " parameters %r\n"
          " measurements %r\n"
          " n_iterations %r\n"
          " n_samples %r\n"
          " n_gmm %r\n"
          " ess_min %r\n"
          " sigma_max %r\n"
          " mercury_build %r\n"
          " output_dir %r\n"
          " test %r\n"
          " verbose %r\n"
          " nodes %r\n"
          " cores %r\n"
          " analysis %r" % (parameters, measurements, n_iterations, n_samples, n_gmm, ess_min, sigma_max,
                            mercury_build, output_dir, test, verbose, nodes, cores, analysis))

    # split measurements
    measurement_names = [d for d in measurements.keys()]
    measurement_data = flatten_list([d['data'] for d in measurements.values()])
    measurement_weights = flatten_list([d['weight'] for d in measurements.values()])
    measurement_solvers = [d['solver'] for d in measurements.values()]
    measurement_outputs = [d['output'] for d in measurements.values()]
    print(measurement_data)
    # split parameters
    parameter_names = [d for d in parameters.keys()]

    # writing data.txt file
    if not os.path.exists("%s/Exp" % output_dir):
        os.makedirs("%s/Exp" % output_dir)
    data_file = "%s/Exp/data.txt" % output_dir
    data = " ".join(str(d) for d in measurement_data)
    open(data_file, 'w').write(data)
    print("Written to %s: %s" % (data_file, data))

    #os.remove(output_dir)

    # iteration of the smc algorithm
    for iteration in range(n_iterations):
        print("Iteration %d" % iteration)

        # define name of output directory, smc table
        sub_dir = "Sim%d" % iteration
        output_dir_iter = "%s/%s" % (output_dir, sub_dir)
        smc_table = '%s/smc_table%d.txt' % (output_dir, iteration)
        smc_table_next = '%s/smc_table%d.txt' % (output_dir, iteration+1)

        if iteration==0 and not os.path.isfile(smc_table):
            print('Create initial parameter table %s' % smc_table)
            smc0 = smc(sigma_max, ess_min, measurement_weights, yadeDataDir=output_dir, simName='data',
                          obsFileName=data_file, loadSamples=False, runYadeInGL=False, standAlone=True)
            # load or generate the initial parameter samples
            smc0.initParams(parameter_names, parameters, n_samples, paramsFile=smc_table,
                               subDir=sub_dir)
            os.system('mv smcTable%d.txt %s' % (iteration, smc_table))

        # run simulations
        if glob.glob(output_dir_iter + "/data_0_*.txt"):
            print("Simulation data already exists in %s" % output_dir_iter)
        else:
            print("Preparing simulations. After simulations have finished, rerun this script to continue.")
            if os.path.isdir(output_dir_iter):
                shutil.rmtree(output_dir_iter)
            runSimulations(smc_table, output_dir_iter, mercury_build, parameter_names, measurement_solvers,
                           nodes, cores, verbose)
            mergeOutputFiles(smc_table, output_dir_iter, measurement_outputs, verbose)

        if not os.path.isfile(smc_table_next) or analysis:
            # stand-alone mode: use GrainLearning as a post-process tool using pre-run DEM data
            smcTest = smc(sigma_max, ess_min, measurement_weights, yadeDataDir=output_dir, simName='data',
                          obsFileName=data_file, loadSamples=True, runYadeInGL=False, standAlone=True)
            # load the initial parameter samples
            smcTest.initParams(parameter_names, parameters, n_samples, paramsFile=smc_table,
                               subDir=sub_dir)
            # initialize the weights
            # include "proposalFile='gmmForCollision_%i.pkl' % iterNO" as a function parameter
            # to take into account proposal probabilities, avoiding bias in resampling
            smcTest.initialize(n_gmm)
            # run sequential Monte Carlo; returns posterior mean and coefficient of variance
            means, covs = smcTest.run()
            # resample parameters
            gmm, maxNumComponents = smcTest.resampleParams(caliStep=-1)
            print("means %r \ncovs %r \ngmm means %r, \nweights %r, \ncovs %r" % (means,covs,gmm.means_,gmm.weights_, gmm.covariances_))

            if os.path.isfile(smc_table_next):
                print("%s already exists" % smc_table_next)
            else:
                print("Create next parameter table")
                os.system('mv smcTable0.txt %s' % smc_table_next)


