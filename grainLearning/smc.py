""" Sequential Monte Carlo: the core of the calibration toolbox
    (https://en.wikipedia.org/wiki/Particle_filter)
"""

import sys, os, glob
import numpy as np
import pickle
from tools import *
from sklearn import mixture
from itertools import repeat

if os.path.exists('YadeSim.py'):
    from YadeSim import createScene, runDEM, addSimData
from multiprocessing import cpu_count
from copy import deepcopy


class smc:
    """
    Base class for sequential Monte Carlo (SMC) filtering
    """

    def __init__(self, sigma, ess, obsWeights,
                 yadeVersion='yade-batch', yadeScript='', yadeDataDir='', threads=0,
                 obsFileName='', obsCtrl='', simDataKeys='', simName='sim',
                 scaleCovWithMax=True, loadSamples=True, runYadeInGL=False, standAlone=True):
        """
        :param sigma: float, default=1.0
            Initial guess of the upper limit of normalized (co)variance coefficient

        :param ess: float, default=0.3
            Effective sample size

        :param obsWeights: ndarray of shape (numObs,)
            Relative weights on observation data e.g., np.ones(numObs)

        :param yadeVersion: string, default='yade-batch'

        :param yadeScript: string
            Name of the python script to run Yade

        :param yadeDataDir: string
            Name of the directory where Yade-DEM data is stored

        :param threads: int, default=1
            Number of threads per simulation

        :param obsFileName: string
            Name of the file where observation data is stored

        :param obsCtrl: string, default=''
            Name of the data sequence to be neglected in Bayesian filtering,
            e.g., strain in strain-controlled triaxial experiments and simulations

        :param simDataKeys: a list of strings, default=['']
            Name of the data sequence to be neglected in Bayesian filtering,
            e.g., strain in strain-controlled triaxial experiments and simulations

        :param simName: string, default='sim'
            Prefix of a simulation file, e.g., <simName>_<key>_<param0>_<param1>_..._<paramN>.txt

        :param loadSamples: bool, default=True
            Whether to load parameter samples from an existing parameter table
            (set to False in the first iteration to create uniform samples from a halton sequence)

        :param scaleCovWithMax: bool, default=True
            Whether to have a time varying covariance matrix or a constant one

        :param runYadeInGL: bool, default=True
            When self.standAlone is false (interactive mode) whether to run Yade within GrainLearning

        :param standAlone: bool, default=True
            Whether to use GrainLearning as a stand-alone tool to postprocess DEM data
        """

        # SMC parameters
        self.sigma = 1.0e6
        self.sigmaMin = 1.0e-4
        self.sigmaMax = sigma
        self.ess = ess
        self.obsWeights = obsWeights
        self.numParams = 0
        self.numSamples = 0
        self.smcSamples = []

        # SMC data
        self.ips = None
        self.covs = None
        self.posterior = None
        self.likelihood = None
        self.proposal = None

        # hyper-parameters of Bayesian Gaussian mixture
        self.__maxNumComponents = 0
        self.__priorWeight = 0
        self.__covType = None
        self.__proposalFile = None

        # DEM configurations
        self.yadeVersion = yadeVersion
        self.yadeScript = yadeScript
        self.yadeDataDir = yadeDataDir
        self.yadeDataSubDirs = []
        self.threads = threads
        self.yadeDataFiles = None
        self.yadeData = None
        self.paramsFiles = []
        self.paramNames = []
        self.paramRanges = {}

        # Control parameters for observations and simulations
        self.obsFileName = obsFileName
        self.obsCtrl = obsCtrl
        self.simDataKeys = simDataKeys
        self.simName = simName

        # Load observation data
        self.obsData, self.obsCtrlData, self.numObs, self.numSteps = self.getObsDataFromFile(obsFileName, obsCtrl)
        # Assume all observation/reference data are independent
        self.__obsMatrix = np.identity(self.numObs)

        # Whether to use GrainLearning as a stand-alone tool to postprocess DEM data
        self.standAlone = standAlone
        # Whether to have a time varying covariance matrix or a constant one (proportional to maximum observation data)
        self.scaleCovWithMax = scaleCovWithMax
        # Whether to load parameter samples from an existing parameter table
        self.loadSamples = loadSamples
        # When self.standAlone is false (interactive mode) whether to run Yade within GrainLearning
        self.runYadeInGL = runYadeInGL

        # if run Yade within GrainLearning in Python, initiate a thread pool and parallel scenes
        self.__pool = None
        self.__scenes = None

    def initParams(self, paramNames, paramRanges, numSamples, paramsFile='TableFromFileNames.txt', subDir=''):
        """
        :param paramNames: list of size (numParams)

        :param paramRanges: dictionary with paramNames being the key

        :param numSamples: int

        :param paramsFile: string
            Initial parameter samples stored in a text file

        :param subDir: string
            The subdirectory where simData are stored
        """

        self.paramNames = paramNames
        self.numParams = len(self.getNames())
        self.yadeDataSubDirs.append(subDir)

        # read in parameters from paramsFile, either generated by a halton sequence or a Gaussian mixture model
        if self.loadSamples:
            print('\nLoading parameter samples from ' + paramsFile + '\n')
            self.numSamples = self.getParamsFromTable(paramsFile)
        # initialize parameter samples uniformly for the first iteration
        else:
            print('\nGenerate parameter samples uniformly from a Halton sequence, '
                  'although a parameter table ' + paramsFile + ' is given.\n(To load ' + paramsFile +
                  ' set loadSamples to True)\n')
            numThreads = self.threads if self.threads else cpu_count()
            self.numSamples = self.getParamsFromHalton(paramRanges, numSamples, numThreads)
            if self.standAlone:
                print('Leaving GrainLearning; only a parameter table is created.')

    def initialize(self, maxNumComponents, priorWeight=0.0, covType='full', proposalFile=''):
        """
        :param maxNumComponents: int, default=numSamples/10

        :param priorWeight: float, default=1./maxNumComponents
            weight_concentration_prior of the BayesianGaussianMixture class
            The dirichlet concentration of each component on the weight distribution (Dirichlet).
            This is commonly called gamma in the literature.
            The higher concentration puts more mass in the center and will lead to more components being active,
            while a lower concentration parameter will lead to more mass at the edge of the mixture weights simplex.
            (https://scikit-learn.org/stable/modules/generated/sklearn.mixture.BayesianGaussianMixture.html)

        :param covType: string, default='full'
            covariance_type of the BayesianGaussianMixture class
            String describing the type of covariance parameters to use. Must be one of:
            'full' (each component has its own general covariance matrix),
            'tied' (all components share the same general covariance matrix),
            'diag' (each component has its own diagonal covariance matrix),
            'spherical' (each component has its own single variance).
            (https://scikit-learn.org/stable/modules/generated/sklearn.mixture.BayesianGaussianMixture.html)

        :param proposalFile: string
            Proposal density trained in the previous iteration and used to generate samples in paramsFile
        """

        # initialize parameters for training Variational Gaussian mixture models
        self.__maxNumComponents = maxNumComponents
        self.__priorWeight = 1. / maxNumComponents if not priorWeight else priorWeight
        self.__covType = covType

        # DEM simulation data
        self.yadeData = np.zeros([self.numSteps, self.numSamples, self.numObs])
        # SMC data (ensemble mean, coefficient of variation, posterior, likelihood, and proposal probability
        self.ips = np.zeros([self.numParams, self.numSteps])
        self.covs = np.zeros([self.numParams, self.numSteps])
        self.posterior = np.zeros([self.numSamples, self.numSteps])
        self.likelihood = np.zeros([self.numSamples, self.numSteps])
        # proposal distribution
        if proposalFile and os.path.exists(proposalFile):
            # load proposal density from file
            self.__proposalFile = proposalFile
            self.proposal = self.loadProposalFromFile(proposalFile, -1)
        else:
            self.proposal = np.ones([self.numSamples]) / self.numSamples

    def getProposalFromSamples(self, iterNO):
        if not self.getSmcSamples():
            RuntimeError("SMC samples not yet loaded...")
        else:
            gmm = mixture.BayesianGaussianMixture(n_components=self.__maxNumComponents,
                                                  weight_concentration_prior=self.__priorWeight, covariance_type='full',
                                                  tol=1e-5, max_iter=int(1e5), n_init=100)
            gmm.fit(self.getSmcSamples()[iterNO])
            proposal = np.exp(gmm.score_samples(self.getSmcSamples()[iterNO]))
            return proposal / sum(proposal)

    def loadProposalFromFile(self, proposalFile, iterNO):
        if not self.getSmcSamples():
            RuntimeError("SMC samples not yet loaded...")
        else:
            # note the encoding 'latin1' is to convert Python bytestring data to Python 3 strings
            # see https://stackoverflow.com/questions/28218466/unpickling-a-python-2-object-with-python-3
            proposalModel = pickle.load(open(proposalFile, 'rb'), encoding='latin1')
            # get normalized samples if one of the means of the Gaussian mixture is smaller than one
            # FIXME the if-statement is there only because I have trained gaussian mixtures not non-dimensionalized
            if np.max(proposalModel.means_) < 1.0:
                samples = self.getNormalizedSamples(iterNO)
            else:
                samples = self.smcSamples[iterNO]
            proposal = np.exp(proposalModel.score_samples(samples))
            proposal *= self.voronoiVols(samples)
            # assign the maximum vol to open regions (use uniform proposal probabilities if Voronoi fails)
            if (proposal < 0.0).all():
                return np.ones(proposal.shape) / self.numSamples
            else:
                proposal[np.where(proposal < 0.0)] = min(proposal[np.where(proposal > 0.0)])
                return proposal / sum(proposal)

    def getNormalizedSamples(self, iterNO):
        """
        normalize parameter samples with respect to the maximums per dimension
        """
        sampleMaxs = np.zeros(self.numParams)
        samples = deepcopy(self.smcSamples[iterNO])
        for i in range(self.numParams):
            sampleMaxs[i] = max(samples[:, i])
            samples[:, i] /= sampleMaxs[i]
        return samples

    def voronoiVols(self, samples):
        from scipy.spatial import Voronoi, ConvexHull
        v = Voronoi(samples)
        vol = np.zeros(v.npoints)
        for i, reg_num in enumerate(v.point_region):
            indices = v.regions[reg_num]
            if -1 in indices:  # some regions can be opened
                vol[i] = -1.0
            else:
                vol[i] = ConvexHull(v.vertices[indices]).volume
        return vol

    def run(self, iterNO=-1, reverse=False):
        """
        Run sequential Monte Carlo in
        1. Stand-alone mode
            (self.standAlone=True)
            - Only postprocess existing DEM data stored in self.YadeDataDir
        2. Interactive mode with Yade running inside GrainLearning
            (self.standAlone=False and self.runYadeInGL=True)
            - Run Yade within GrainLearning using a thread pool
            - Postprocess in memory without writing the data into text files
        3. Interactive mode with Yade running outside GrainLearning
            (self.standAlone=False and self.runYadeInGL=False)
            - Run Yade in batch mode with parameter samples stored in a text file
            - Postprocess the data stored in self.YadeDataDir

        :param iterNO: int, default=-1
            Index of self.smcSamples, i.e., iteration NO.

        :param reverse: bool, default=False
            If iteration number is an odd number, reverse the data sequences to ensure data continuity

        :return:
            self.ips: ndarray of shape (self.numParams, self.numSteps)
                Ensemble mean as the identified parameters

            self.covs: ndarray of shape (self.numParams, self.numSteps)
                Ensemble coefficient of variation (ensemble mean / ensemble variance)
        """

        # if iterate Bayesian filtering, reload the observation data
        if iterNO > 0:
            print('*** At the iteration NO. %i, with sigma=%f ***\n' % (iterNO, self.sigma))
            self.obsData, self.obsCtrlData, self.numObs, self.numSteps = \
                self.getObsDataFromFile(self.obsFileName, self.obsCtrl)

        # if use GrainLearning as a stand-alone tool (simData already exist beforehand)
        if self.standAlone:
            print('*** Read in pre-existing DEM simulation data now ***\n')
            self.checkParamsError()
            self.getYadeData()
        # if run DEM simulations with GrainLearning before the Bayesian filtering steps
        else:
            if self.runYadeInGL:
                print('*** Run Yade-DEM simulations now within GrainLearning ***\n')
                self.runYadePython(iterNO)
            else:
                self.runYadeBatch(iterNO)
                self.getYadeDataFilesFromSamples()
                self.checkParamsError()
                self.getYadeData()

        # iterate Bayesian filtering until the effective sample size is sufficient
        if reverse:
            self.obsCtrlData = self.obsCtrlData[::-1]
            self.obsData = self.obsData[::-1, :]
            self.yadeData = self.yadeData[::-1, :, :]
        self.runESSLoop()
        # turn off the stand-alone mode after completing an iteration
        self.standAlone = False

        # calculate effective sample size
        ess = self.getEffectiveSampleSize()[-1]
        print('\nEffective sample size: %f;' % ess, 'Normalized covariance coefficient: %f\n' % self.sigma)
        return self.ips, self.covs

    def checkParamsError(self):
        """
        Check if each parameter sample matches its simData file name
        """
        # check if parameter combinations match with the simulation filename.
        for i, f in enumerate(self.yadeDataFiles):
            # get the file name fore the suffix
            f = f.split('.' + f.split('.')[-1])[0]
            # get parameters from the remaining string
            paramsString = f.split('_')[-self.numParams:]
            # element wise comparison of the parameter vector
            if not (np.abs((np.float64(paramsString) - self.getSmcSamples()[-1][i])
                           / self.getSmcSamples()[-1][i] < 1e-10).all()):
                raise RuntimeError(
                    "Parameters " + ", ".join(
                        ["%s" % v for v in self.getSmcSamples()[-1][i]]) + " do not match with the data file name " + f)

    def runYadePython(self, iterNO=-1):
        """
        Run Yade interactive within GrainLearning using a thread pool
        """
        # get parameter list
        paramsList = []
        for i in range(self.numSamples):
            paramsForEach = {}
            for j, name in enumerate(self.paramNames):
                paramsForEach[name] = self.smcSamples[iterNO][i][j]
            paramsForEach['key'] = i
            paramsList.append(paramsForEach)
        # pass parameter list to simulation instances FIXME: runDEM is the user-defined Yade script
        self.__pool = get_pool(mpi=False, threads=cpu_count())
        self.__scenes = self.__pool.map(createScene, range(self.numSamples))
        simData = self.__pool.map(runDEM, zip(self.__scenes, paramsList, repeat(self.obsCtrlData)))
        self.__pool.close()
        # ~ data = runDEM([self.smc__scenes,paramsList[0]])
        # get observation and simulation data ready for Bayesian filtering
        self.obsData = np.array([self.obsData[name] for name in self.simDataKeys]).transpose()
        for i, data in enumerate(simData):
            for j, name in enumerate(self.simDataKeys):
                # ~ print len(data[name]),i
                self.yadeData[:, i, j] = data[name]
        # ~ print np.linalg.norm(data[self.obsCtrl]-self.obsCtrlData)

    def runYadeBatch(self, iterNO=-1):
        """
        Run Yade-batch with parameter samples in paramsFile[iterNO]
        """
        self.yadeDataSubDirs.append('iter%i' % iterNO)
        yadeDataDir = self.yadeDataDir + '/' + self.yadeDataSubDirs[-1]
        if not os.path.exists(yadeDataDir):
            os.mkdir(yadeDataDir)
        else:
            input('simData directory already exists (%i files). Delete?\n' % len(glob.glob(yadeDataDir + '/*')))
            os.system('rm ' + yadeDataDir + '/*')
        input("*** Please check if the yade script is correct... ***\n"
              + "\nAbout to run Yade in batch mode using '" +
              " ".join([self.yadeVersion, self.paramsFiles[iterNO], self.yadeScript]) +
              "'\n(Hit enter if the above is correct)\n")
        os.system(' '.join([self.yadeVersion, self.paramsFiles[iterNO], self.yadeScript]))
        input('All simulations have finished. Now return to GrainLearning?\n')
        os.system('mv ' + self.simName + '*.txt ' + yadeDataDir)

    def subRun(self, sigma):
        self.sigma = sigma
        for i in range(self.numSteps):
            self.likelihood[:, i], self.posterior[:, i], self.ips[:, i], self.covs[:, i] = self.recursiveBayesian(i)
        return self.ess - self.getEffectiveSampleSize()[-1]

    def runESSLoop(self, essTol=1.0e-2):
        from scipy import optimize
        """
        Iterate Bayesian filtering until the effective sample size is sufficient
        """
        # get test solutions at sigmaMin and sigmaMax
        evalSigmaMin = self.subRun(self.sigmaMin)
        evalSigmaMax = self.subRun(self.sigmaMax)
        # increase sigmaMin if it is too small
        while isnan(evalSigmaMin):
            self.sigmaMin *= 1.1
            evalSigmaMin = self.subRun(self.sigmaMin)
        # if evaluations with sigmaMin and sigmaMax have the same sign, use sigmaMin anyway
        if evalSigmaMin * evalSigmaMax > 0:
            self.subRun(self.sigmaMin)
        
        # if a proposal distribution is considered when resampling    
        if not self.__proposalFile:
            # reinitialize normalized covariance coefficient if the effective sample size is too small
            while True:
                ess0 = self.subRun(self.sigmaMax)
                if ess0 > 0:
                    self.sigmaMax = float(input("\nReinitialize The maximum normalized covariance coefficient "
                                                "such that the effective sample size is large enough"
                                                "\nCurrent sigma is %f and effective sample size is %f"
                                                "\nSigma = "
                                                % (self.sigma, -ess0 + self.ess)))
                else:
                    break
            # solve sigma for the effective sample size equals self.ess
            if self.subRun(self.sigmaMin)<0:
                print("Warning: ess(sigmaMin)>essMin; decrease sigmaMin to get better convergence")
                sigma = self.sigmaMin
            elif self.subRun(self.sigmaMax)>0:
                raise Warning("ess(sigmaMax)<essMin; increase sigmaMax to get better convergence")
                sigma = self.sigmaMax
            else:
                sigma = optimize.brentq(self.subRun, self.sigmaMin, self.sigmaMax, xtol=essTol)
            self.subRun(sigma)
        else:
            # find the normalized covariance coefficient that maximizes the effective sample size
            soln = optimize.minimize(self.subRun, x0=0.5 * (self.sigmaMin + self.sigmaMax), tol=essTol)
            sigma = soln.x
            ess0 = self.subRun(sigma)
            # if the effective sample size is larger than self.ess solve sigma again
            if ess0 < 0:
                sigma = optimize.brentq(self.subRun, self.sigmaMin, self.sigma, xtol=essTol)
                self.subRun(sigma)
        # update the upper limit of normalized (co)variance coefficient
        self.sigmaMax = sigma

    def getYadeDataFiles(self, iterNO=0):
        yadeDataDir = os.getcwd() + '/' + self.yadeDataDir + '/' + self.yadeDataSubDirs[iterNO]
        yadeDataFiles = glob.glob(yadeDataDir + '/*' + self.simName + '*')
        # if nothing in the directory
        if not os.path.exists(yadeDataDir) and yadeDataFiles:
            print(yadeDataDir + " is empty\n")
            return 0

        # if self.simName is incorrect and thus cannot get the simulation data files
        while not yadeDataFiles:
            print("No simulation files can be found. Do you have them stored in ./" + self.yadeDataDir +
                  "? \nPlease give the correct 'simName' so that I can search...\n")
            self.simName = input("simName: ")
            yadeDataFiles = glob.glob(os.getcwd() + '/' +
                                      self.yadeDataDir + '/' + self.yadeDataSubDirs[iterNO] +
                                      '/*' + self.simName + '*')
        # sort yadeDataFiles in ascending order with respect to the key (first string separated by _ after simName)
        ids = np.argsort([int((f.split(yadeDataDir)[1].split('_'))[1]) for f in yadeDataFiles])
        self.yadeDataFiles = [yadeDataFiles[i] for i in ids]
        num = len(self.yadeDataFiles)
        print("Found %i" % num + " files in " + self.yadeDataDir + '/' + self.yadeDataSubDirs[iterNO] + '\n')
        return num

    def getYadeDataFilesFromSamples(self):
        self.yadeDataFiles = []
        for i in range(self.numSamples):
            fileName = self.yadeDataDir + '/' + self.yadeDataSubDirs[-1] + '/' + self.simName + '*_%i_*.txt' % i
            files = glob.glob(fileName)
            if not files:
                raise RuntimeError("No files with name " + fileName + ' found')
            elif len(files) > 1:
                raise RuntimeError("Found more than 1 file with name " + fileName)
            self.yadeDataFiles.append(files[0])

    def getYadeData(self):
        """
        Read simulation data into yadeData and drop the keys of obsData
        """
        if 0 in self.yadeData.shape:
            raise RuntimeError("number of Observations, samples or steps undefined!")
        for i, f in enumerate(self.yadeDataFiles):
            # if do not have the data that control the simulation
            if not self.obsCtrl:
                yadeData = np.genfromtxt(f)
                for j in range(self.numObs):
                    self.yadeData[:, i, j] = yadeData
            else:
                yadeData = getKeysAndData(f)
                for j, key in enumerate(self.obsData.keys()):
                    self.yadeData[:, i, j] = yadeData[key]
        # Remove the keys from obsData
        if self.obsCtrl != '':
            obsData = np.zeros([self.numSteps, self.numObs])
            for j, key in enumerate(self.obsData.keys()):
                obsData[:, j] = self.obsData[key]
            self.obsData = obsData

    def recursiveBayesian(self, caliStep, iterNO=-1):
        """
        Recursive Bayesian for each data assimilation step
        See https://en.wikipedia.org/wiki/Recursive_Bayesian_estimation for details

        :param caliStep: int
            Calibration or data assimilation step

        :param iterNO: int, default=-1
            Index of self.smcSamples, i.e., iteration NO.

        :return:
            likelihood: float

            posterior: float

            ips: ndarray of shape (self.numParams,)
                Ensemble mean as the identified parameters

            covs: ndarray of shape (self.numParams,)
                Ensemble mean as the identified parameters
        """

        # compute the current likelihood and update posterior probability
        likelihood = self.getLikelihood(caliStep)
        posterior = self.update(caliStep, likelihood)

        # get ensemble averages and coefficients of variance
        ips = np.zeros(self.numParams)
        covs = np.zeros(self.numParams)
        for i in range(self.numParams):
            # ensemble average
            ips[i] = self.smcSamples[iterNO][:, i].dot(posterior)
            # diagonal variance
            covs[i] = ((self.smcSamples[iterNO][:, i] - ips[i]) ** 2).dot(posterior)
            # get the coefficient of variance
            covs[i] = np.sqrt(covs[i]) / ips[i]
        return likelihood, posterior, ips, covs

    def getLikelihood(self, caliStep):
        """
        Compute the likelihood at the data assimilation step caliStep using the multivariate Gaussian distribution
        see https://en.wikipedia.org/wiki/Multivariate_normal_distribution for details

        :param caliStep: int
            Calibration or data assimilation step

        :return: likelihood: float, p(y_t|x_t)
        """

        # state vector y_t = H(x_t)+Sigma_t
        stateVec = self.yadeData[caliStep, :, :].dot(self.__obsMatrix)
        obsVec = self.obsData[caliStep, :]

        # row-wise subtraction obsVec[numObs]-stateVec[numSamples,numObs]
        vecDiff = obsVec - stateVec
        Sigma = self.getCovMatrix(caliStep, self.obsWeights)
        invSigma = np.linalg.inv(Sigma)
        likelihood = np.zeros(self.numSamples)

        # compute likelihood = exp(-0.5*(y_t-H(x_t))*Sigma_t^{-1}*(y_t-H(x_t)))
        for i in range(self.numSamples):
            power = (vecDiff[i, :]).dot(invSigma.dot(vecDiff[i, :].T))
            likelihood[i] = np.exp(-0.5 * power)

        # regularize likelihood
        likelihood /= np.sum(likelihood)
        return likelihood

    def update(self, caliStep, likelihood):
        """
        Update posterior probability according to the recursive Bayes' rule
        """
        posterior = np.zeros(self.numSamples)
        if caliStep == 0:
            posterior = likelihood / self.proposal
        else:
            posterior = self.posterior[:, caliStep - 1] * likelihood

        # regularize likelihood
        posterior /= np.sum(posterior)
        return posterior

    def getCovMatrix(self, caliStep, weights):
        """
        Compute the covariance matrix either varying in time or proportional to maximum observation data

        :param caliStep: int
            Calibration or data assimilation step

        :param weights: ndarray of shape (numObs,)
            Relative weights on observation data e.g., np.ones(numObs)

        :return: Sigma: ndarray of shape (numObs, numObs)
            TODO The covariance matrix is assumed to be diagonal which is not necessarily true.
            The covariance matrix, assumed to be diagonal
        """

        Sigma = np.zeros([self.numObs, self.numObs])
        # scale observation data with normalized variance parameter to get covariance matrix
        for i in range(self.numObs):
            # use smaller weights for higher precision
            if self.scaleCovWithMax:
                Sigma[i, i] = self.sigma * weights[i] * max(self.obsData[:, i]) ** 2
            else:
                Sigma[i, i] = self.sigma * weights[i] * self.obsData[caliStep, i] ** 2
        return Sigma

    def getParamsFromHalton(self, paramRanges, numSamples, threads):
        """
        Generate the initial parameter sample using a halton sequence

        :param paramRanges: dictionary with paramNames being the keys

        :param numSamples: int

        :param threads: int

        :return: number of parameter samples
        """

        if not paramRanges:
            raise RuntimeError(
                "Parameter range not given. Define the dictionary-type paramRanges or set loadSamples True")
        self.paramRanges = paramRanges

        # get parameter names and upper and lower bounds
        names = self.getNames()
        minsAndMaxs = np.array([paramRanges[key] for key in names])
        mins = minsAndMaxs[:, 0]
        maxs = minsAndMaxs[:, 1]
        print('Parameters to be identified:', ", ".join(names), '\nMins:', mins, '\nMaxs:', maxs, '\n')

        initSmcSamples, initparamsFile = initParamsTable(keys=names, maxs=maxs, mins=mins, num=numSamples,
                                                         threads=threads)
        self.smcSamples.append(initSmcSamples)
        self.paramsFiles.append(initparamsFile)
        return numSamples

    def getParamsFromTable(self, paramsFile):
        """
        :param paramsFile: string
            Current parameter table in a text file

        :return: number of parameter samples and number of unknown parameters
        """

        # switch to the stand-alone mode after getting a nonzero number of simData files
        if self.yadeDataSubDirs[0] and self.getYadeDataFiles():
            self.standAlone = True

        # if a paramsFile exist, read in parameters directly
        if os.path.exists(paramsFile):
            # FIXME The following assumes unknown parameters in the last self.numParams columns. How to generalize?
            smcSamples = np.genfromtxt(paramsFile, comments='!')[:, -self.numParams:]
            numSamples = smcSamples.shape[0]
            # TODO Still check whether samples read from the paramsFile and simData fileNames are the same

        # if cannot find a paramsFile, extract parameter values from the names of simulation data files
        elif not os.path.exists(paramsFile) and self.yadeDataFiles is not None:
            # initialize the current smcSamples
            print('Cannot find your parameter table ' + paramsFile +
                  '. Will try to get them from the names of simData files...\n')
            numSamples = len(self.yadeDataFiles)
            smcSamples = np.zeros([numSamples, self.numParams])
            for i, f in enumerate(self.yadeDataFiles):
                # get the file type (e.g., txt)
                suffix = '.' + f.split('.')[-1]
                # get the string before the file type
                f = f.split(suffix)[0]
                # get parameters from the remaining string
                # FIXME The following assumes the last self.numParams values is the parameter sample. How to generalize?
                paramsString = f.split('_')[-self.numParams:]
                # if the number of strings after <simName> and <key> is different from self.numParams, throw an error
                if len(f.split('_')[2:]) != self.numParams:
                    RuntimeError(
                        'Number of parameters extracted from the file name is different from self.numParams\n\
                         Check if your simulation data file is named as <simName>_<key>_<param0>_<param1>_..._<paramN>.txt')
                # convert strings to float numbers
                smcSamples[i, :] = np.float64(paramsString)
            # write extract parameter values into a text file
            writeToTable(paramsFile, smcSamples, self.numParams, numSamples, self.threads, self.paramNames)
        else:
            raise RuntimeError("Nether the parameter table " + paramsFile +
                               ' nor simData files exist in ' + self.yadeDataDir)

        # append parameter samples to self.smcSamples
        self.paramsFiles.append(paramsFile)
        self.smcSamples.append(smcSamples)
        return numSamples

    def getObsDataFromFile(self, obsFileName, obsCtrl):
        # if do not know the data that control the simulations
        if not self.obsCtrl:
            keysAndData = np.genfromtxt(obsFileName)
            # if only one observation data vector exist, reshape it with [numSteps,1]
            if len(keysAndData.shape) == 0:
                keysAndData = keysAndData.reshape([1, 1])
            elif len(keysAndData.shape) == 1:
                keysAndData = keysAndData.reshape([keysAndData.shape[0], 1])
            return keysAndData, None, keysAndData.shape[1], keysAndData.shape[0]
        else:
            keysAndData = getKeysAndData(obsFileName)
            # separate the control data sequence from the observation data
            obsCtrlData = keysAndData.pop(obsCtrl)
            numSteps = len(obsCtrlData)
            numObs = len(keysAndData.keys())
            return keysAndData, obsCtrlData, numObs, numSteps

    def resampleParams(self, caliStep, iterNO=-1):
        """
        Resample parameters using a variational Gaussian mixture model
        """
        names = self.getNames()
        smcSamples = self.smcSamples[iterNO]
        numSamples = self.numSamples
        numThreads = self.threads if self.threads else cpu_count()
        # posterior probability at caliStep is used as the proposal distribution
        proposal = self.posterior[:, caliStep]
        newSmcSamples, newparamsFile, gmm, maxNumComponents = \
            resampledParamsTable(keys=names, smcSamples=smcSamples, proposal=proposal, num=numSamples,
                                 threads=numThreads,
                                 maxNumComponents=self.__maxNumComponents, priorWeight=self.__priorWeight,
                                 covType=self.__covType,
                                 tableName='smcTable%i.txt' % (iterNO + 1))
        self.smcSamples.append(newSmcSamples)
        self.paramsFiles.append(newparamsFile)
        return gmm, maxNumComponents

    def getPosterior(self):
        return self.posterior

    def getSmcSamples(self):
        return self.smcSamples

    def getNumSteps(self):
        return self.numSteps

    def getEffectiveSampleSize(self):
        nEff = 1. / sum(self.getPosterior() ** 2)
        return nEff / self.numSamples

    def getNames(self):
        return self.paramNames

    def getObsData(self):
        return np.hstack((self.obsCtrlData.reshape(self.numSteps, 1), self.obsData))

    def trainGMMinTime(self, iterNO=-1):
        """
        Train a Gaussian mixture model at every calibration step

        :param iterNO: int, default=-1
            Index of self.smcSamples, i.e., iteration NO.

        :return: gmmList, list of Gaussian mixture models
        """
        gmmList = []
        smcSamples = self.smcSamples[iterNO]
        for i in range(self.numSteps):
            print('Train DP mixture at time %i' % i)
            posterior = self.posterior[:, i]
            gmmList.append(getGMMFromPosterior(smcSamples, posterior,
                                               self.__maxNumComponents, self.__priorWeight, self.__covType))
        return gmmList

    def removeDegeneracy(self, caliStep=-1, threshold=0.5):
        """
        Remove samples that have weights smaller than a threshold and rerun recursive Bayesian

        :param caliStep: int
            Calibration or data assimilation step

        :param threshold: float
            A threshold below which the samples are removed from the ensemble
        """
        effIDs = np.where(self.posterior[:, caliStep] < threshold)[0]
        self.proposal = self.proposal[effIDs, :]
        self.likelihood = self.likelihood[effIDs, :]
        self.posterior = self.posterior[effIDs, :]
        self.smcSamples[0] = self.smcSamples[0][effIDs, :]
        self.yadeData = self.yadeData[:, effIDs, :]
        self.numSamples = len(effIDs)
        for i in range(self.numSteps):
            self.likelihood[:, i], self.posterior[:, i], \
            self.ips[:, i], self.covs[:, i] = self.recursiveBayesian(i, self.proposal[:, i])

    def writeBayeStatsToFile(self, reverse):
        np.savetxt(self.yadeDataDir + '/samples.txt', self.getSmcSamples()[0])
        np.savetxt(self.yadeDataDir + '/ips.txt', self.ips[:, ::(-1) ** reverse].T)
        np.savetxt(self.yadeDataDir + '/weights.txt', self.getPosterior()[:, ::(-1) ** reverse])
