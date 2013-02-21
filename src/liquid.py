""" @package liquid Matching of liquid bulk properties.  Under development.

@author Lee-Ping Wang
@date 04/2012
"""

import os
import shutil
from nifty import *
from target import Target
import numpy as np
from molecule import Molecule
from re import match, sub
import subprocess
from subprocess import PIPE
try:
    from lxml import etree
except: pass
from pymbar import pymbar
import itertools
from collections import defaultdict, namedtuple
from optimizer import Counter
import csv

def weight_info(W, PT, N_k, verbose=True):
    C = []
    N = 0
    W += 1.0e-300
    I = np.exp(-1*np.sum((W*np.log(W))))
    for ns in N_k:
        C.append(sum(W[N:N+ns]))
        N += ns
    C = np.array(C)
    if verbose:
        print "MBAR Results for Phase Point %s, Box, Contributions:" % str(PT)
        print C
        print "InfoContent: % .2f snapshots (%.2f %%)" % (I, 100*I/len(W))
    return C

# NPT_Trajectory = namedtuple('NPT_Trajectory', ['fnm', 'Rhos', 'pVs', 'Energies', 'Grads', 'mEnergies', 'mGrads', 'Rho_errs', 'Hvap_errs'])

class Liquid(Target):
    
    """ Subclass of Target for liquid property matching."""
    
    def __init__(self,options,tgt_opts,forcefield):
        """Instantiation of the subclass.

        We begin by instantiating the superclass here and also
        defining a number of core concepts for energy / force
        matching.

        @todo Obtain the number of true atoms (or the particle -> atom mapping)
        from the force field.
        """
        
        # Initialize the SuperClass!
        super(Liquid,self).__init__(options,tgt_opts,forcefield)
        # Fractional weight of the density
        self.set_option(tgt_opts,'w_rho',forceprint=True)
        # Fractional weight of the enthalpy of vaporization
        self.set_option(tgt_opts,'w_hvap',forceprint=True)
        # Fractional weight of the thermal expansion coefficient
        self.set_option(tgt_opts,'w_alpha',forceprint=True)
        # Fractional weight of the isothermal compressibility
        self.set_option(tgt_opts,'w_kappa',forceprint=True)
        # Fractional weight of the isobaric heat capacity
        self.set_option(tgt_opts,'w_cp',forceprint=True)
        # Fractional weight of the dielectric constant
        self.set_option(tgt_opts,'w_eps0',forceprint=True)
        # Optionally pause on the zeroth step
        self.set_option(tgt_opts,'manual')
        # Don't target the average enthalpy of vaporization and allow it to freely float (experimental)
        self.set_option(tgt_opts,'hvap_subaverage')
        
        #======================================#
        #     Variables which are set here     #
        #======================================#
        
        ## Read the reference data
        self.read_data()
        ## Prepare the temporary directory
        self.prepare_temp_directory(options,tgt_opts)
        #======================================#
        #          UNDER DEVELOPMENT           #
        #======================================#
        # Put stuff here that I'm not sure about. :)
        np.set_printoptions(precision=4, linewidth=100)
        np.seterr(under='ignore')
        ## Saved force field mvals for all iterations
        self.SavedMVal = {}
        ## Saved trajectories for all iterations and all temperatures :)
        self.SavedTraj = defaultdict(dict)
        ## Evaluated energies for all trajectories (i.e. all iterations and all temperatures), using all mvals
        self.MBarEnergy = defaultdict(lambda:defaultdict(dict))

    def read_data(self):
        # Read the 'data.csv' file. The file should contain guidelines.
        with open(os.path.join(self.tgtdir,'data.csv'),'rU') as f: R0 = list(csv.reader(f))
        # All comments are erased.
        R1 = [[sub('#.*$','',word) for word in line] for line in R0 if len(line[0]) > 0 and line[0][0] != "#"]
        # All empty lines are deleted and words are converted to lowercase.
        R = [[wrd.lower() for wrd in line] for line in R1 if any([len(wrd) for wrd in line]) > 0]
        global_opts = OrderedDict()
        found_headings = False
        known_vars = ['mbar','rho','hvap','alpha','kappa','cp','eps0','cvib_intra','cvib_inter','cni','devib_intra','devib_inter']
        self.RefData = OrderedDict()
        for line in R:
            if line[0] == "global":
                # Global options are mainly denominators for the different observables.
                if isfloat(line[2]):
                    global_opts[line[1]] = float(line[2])
                elif line[2].lower() == 'false':
                    global_opts[line[1]] = False
                elif line[2].lower() == 'true':
                    global_opts[line[1]] = True
            elif not found_headings:
                found_headings = True
                headings = line
                if len(set(headings)) != len(headings):
                    raise Exception('Column headings in data.csv must be unique')
                if 'p' not in headings:
                    raise Exception('There must be a pressure column heading labeled by "p" in data.csv')
                if 't' not in headings:
                    raise Exception('There must be a temperature column heading labeled by "t" in data.csv')
            elif found_headings:
                try:
                    # Temperatures are in kelvin.
                    t     = [float(val) for head, val in zip(headings,line) if head == 't'][0]
                    # For convenience, users may input the pressure in atmosphere or bar.
                    pval  = [float(val.split()[0]) for head, val in zip(headings,line) if head == 'p'][0]
                    punit = [val.split()[1] if len(val.split()) >= 1 else "atm" for head, val in zip(headings,line) if head == 'p'][0]
                    unrec = set([punit]).difference(['atm','bar']) 
                    if len(unrec) > 0:
                        raise Exception('The pressure unit %s is not recognized, please use bar or atm' % unrec[0])
                    # This line actually reads the reference data and inserts it into the RefData dictionary of dictionaries.
                    for head, val in zip(headings,line):
                        if head == 't' or head == 'p' : continue
                        if isfloat(val):
                            self.RefData.setdefault(head,OrderedDict([]))[(t,pval,punit)] = float(val)
                        elif val.lower() == 'true':
                            self.RefData.setdefault(head,OrderedDict([]))[(t,pval,punit)] = True
                        elif val.lower() == 'false':
                            self.RefData.setdefault(head,OrderedDict([]))[(t,pval,punit)] = False
                except:
                    print line
                    raise Exception('Encountered an error reading this line!')
            else:
                print line
                raise Exception('I did not recognize this line!')
        # Check the reference data table for validity.
        default_denoms = defaultdict(int)
        PhasePoints = None
        for head in self.RefData:
            if head not in known_vars+[i+"_wt" for i in known_vars]:
                # Only hard-coded properties may be recognized.
                raise Exception("The column heading %s is not recognized in data.csv" % head)
            if head in known_vars:
                if head+"_wt" not in self.RefData:
                    # If the phase-point weights are not specified in the reference data file, initialize them all to one.
                    self.RefData[head+"_wt"] = OrderedDict([(key, 1.0) for key in self.RefData[head]])
                wts = np.array(self.RefData[head+"_wt"].values())
                dat = np.array(self.RefData[head].values())
                avg = np.average(dat, weights=wts)
                if len(wts) > 1:
                    # If there is more than one data point, then the default denominator is the
                    # standard deviation of the experimental values.
                    default_denoms[head+"_denom"] = np.sqrt(np.dot(wts, (dat-avg)**2)/wts.sum())
                else:
                    # If there is only one data point, then the denominator is just the single
                    # data point itself.
                    default_denoms[head+"_denom"] = np.sqrt(dat[0])
            self.PhasePoints = self.RefData[head].keys()
            # This prints out all of the reference data.
            # printcool_dictionary(self.RefData[head],head)
        # Create labels for the directories.
        self.Labels = ["%.2fK-%.1f%s" % i for i in self.PhasePoints]
        # print global_opts
        # print default_denoms
        for opt in global_opts:
            if "_denom" in opt:
                # Record entries from the global_opts dictionary so they can be retrieved from other methods.
                self.set_option(global_opts,opt,default=default_denoms[opt])
            else:
                self.set_option(global_opts,opt)

    def indicate(self):
        # Somehow the "indicator" functionality made its way into "get".  No matter.
        return

    def objective_term(self, points, expname, calc, err, grad, name="Quantity", SubAverage=False):
        if expname in self.RefData:
            exp = self.RefData[expname]
            Weights = self.RefData[expname+"_wt"]
            Denom = getattr(self,expname+"_denom")
        else:
            # If the reference data doesn't exist then return nothing.
            return 0.0, np.zeros(self.FF.np, dtype=float), np.zeros((self.FF.np,self.FF.np),dtype=float), None
            
        Sum = sum(Weights.values())
        for i in Weights:
            Weights[i] /= Sum
        print "Weights have been renormalized to", sum(Weights.values())
        # Use least-squares or hyperbolic (experimental) objective.
        LeastSquares = True

        print "Physical quantity %s uses denominator = % .4f" % (name, Denom)
        if not LeastSquares:
            # If using a hyperbolic functional form
            # we still want the contribution to the 
            # objective function to be the same when
            # Delta = Denom.
            Denom /= 3 ** 0.5
        
        Objective = 0.0
        Gradient = np.zeros(self.FF.np, dtype=float)
        Hessian = np.zeros((self.FF.np,self.FF.np),dtype=float)
        Objs = {}
        GradMap = []
        avgCalc = 0.0
        avgExp  = 0.0
        avgGrad = np.zeros(self.FF.np, dtype=float)
        for i, PT in enumerate(points):
            avgCalc += Weights[PT]*calc[PT]
            avgExp  += Weights[PT]*exp[PT]
            avgGrad += Weights[PT]*grad[PT]
        for i, PT in enumerate(points):
            if SubAverage:
                G = grad[PT]-avgGrad
                Delta = calc[PT] - exp[PT] - avgCalc + avgExp
            else:
                G = grad[PT]
                Delta = calc[PT] - exp[PT]
            if LeastSquares:
                # Least-squares objective function.
                ThisObj = Weights[PT] * Delta ** 2 / Denom**2
                Objs[PT] = ThisObj
                ThisGrad = 2.0 * Weights[PT] * Delta * G / Denom**2
                GradMap.append(G)
                Objective += ThisObj
                Gradient += ThisGrad
                # Gauss-Newton approximation to the Hessian.
                Hessian += 2.0 * Weights[PT] * (np.outer(G, G)) / Denom**2
            else:
                # L1-like objective function.
                D = Denom
                S = Delta**2 + D**2
                ThisObj  = Weights[PT] * (S**0.5-D) / Denom
                ThisGrad = Weights[PT] * (Delta/S**0.5) * G / Denom
                ThisHess = Weights[PT] * (1/S**0.5-Delta**2/S**1.5) * np.outer(G,G) / Denom
                Objs[PT] = ThisObj
                GradMap.append(G)
                Objective += ThisObj
                Gradient += ThisGrad
                Hessian += ThisHess
        GradMapPrint = [["#PhasePoint"] + self.FF.plist]
        for PT, g in zip(points,GradMap):
            GradMapPrint.append([' %8.2f %8.1f %3s' % PT] + ["% 9.3e" % i for i in g])
        o = open('gradient_%s.dat' % name,'w')
        for line in GradMapPrint:
            print >> o, ' '.join(line)
        o.close()
            
        Delta = np.array([calc[PT] - exp[PT] for PT in points])
        delt = {PT : r for PT, r in zip(points,Delta)}
        print_out = OrderedDict([('    %8.2f %8.1f %3s' % PT,"%9.3f    %9.3f +- %-7.3f % 7.3f % 9.5f % 9.5f" % (exp[PT],calc[PT],err[PT],delt[PT],Weights[PT],Objs[PT])) for PT in calc])
        return Objective, Gradient, Hessian, print_out

    def submit_jobs(self, mvals, AGrad=True, AHess=True):
        # This routine is called by Objective.stage() will run before "get".
        # It submits the jobs to the Work Queue and the stage() function will wait for jobs to complete.
        #
        # First dump the force field to a pickle file
        with open('forcebalance.p','w') as f: lp_dump((self.FF,mvals,self.h,AGrad),f)

        # Give the user an opportunity to copy over data from a previous (perhaps failed) run.
        if Counter() == 0 and self.manual:
            warn_press_key("Now's our chance to fill the temp directory up with data!")

        # Set up and run the NPT simulations.
        for label, pt in zip(self.Labels, self.PhasePoints):
            T = pt[0]
            P = pt[1]
            Punit = pt[2]
            if Punit == 'bar':
                P *= 1.0 / 1.01325
            if not os.path.exists(label):
                os.makedirs(label)
                os.chdir(label)
                self.npt_simulation(T,P)
                os.chdir('..')

    def get(self, mvals, AGrad=True, AHess=True):
        
        """
        Fitting of liquid bulk properties.  This is the current major
        direction of development for ForceBalance.  Basically, fitting
        the QM energies / forces alone does not always give us the
        best simulation behavior.  In many cases it makes more sense
        to try and reproduce some experimentally known data as well.

        In order to reproduce experimentally known data, we need to
        run a simulation and compare the simulation result to
        experiment.  The main challenge here is that the simulations
        are computationally intensive (i.e. they require energy and
        force evaluations), and furthermore the results are noisy.  We
        need to run the simulations automatically and remotely
        (i.e. on clusters) and a good way to calculate the derivatives
        of the simulation results with respect to the parameter values.

        This function contains some experimentally known values of the
        density and enthalpy of vaporization (Hvap) of liquid water.
        It launches the density and Hvap calculations on the cluster,
        and gathers the results / derivatives.  The actual calculation
        of results / derivatives is done in a separate file.

        After the results come back, they are gathered together to form
        an objective function.

        @param[in] mvals Mathematical parameter values
        @param[in] AGrad Switch to turn on analytic gradient
        @param[in] AHess Switch to turn on analytic Hessian
        @return Answer Contribution to the objective function
        
        """

        Answer = {}

        Results = {}
        Points = []  # These are the phase points for which data exists.
        BPoints = [] # These are the phase points for which we are doing MBAR for the condensed phase.
        mPoints = [] # These are the phase points to use for enthalpy of vaporization; if we're scanning pressure then set hvap_wt for higher pressures to zero.
        tt = 0
        for label, PT in zip(self.Labels, self.PhasePoints):
            if os.path.exists('./%s/npt_result.p.bz2' % label):
                os.system('bunzip2 ./%s/npt_result.p.bz2' % label)
            if os.path.exists('./%s/npt_result.p' % label):
                Points.append(PT)
                Results[tt] = lp_load(open('./%s/npt_result.p' % label))
                if 'hvap' in self.RefData and PT[0] not in [i[0] for i in mPoints]:
                    mPoints.append(PT)
                if 'mbar' in self.RefData and self.RefData['mbar'][PT]:
                    BPoints.append(PT)
                tt += 1
            else:
                for obs in self.RefData:
                    del self.RefData[obs][PT]

        # Assign variable names to all the stuff in npt_result.p
        Rhos, Vols, Energies, Dips, Grads, GDips, mEnergies, mGrads, \
            Rho_errs, Hvap_errs, Alpha_errs, Kappa_errs, Cp_errs, Eps0_errs = ([Results[t][i] for t in range(len(Points))] for i in range(14))
        
        R  = np.array(list(itertools.chain(*list(Rhos))))
        V  = np.array(list(itertools.chain(*list(Vols))))
        E  = np.array(list(itertools.chain(*list(Energies))))
        Dx = np.array(list(itertools.chain(*list(d[:,0] for d in Dips))))
        Dy = np.array(list(itertools.chain(*list(d[:,1] for d in Dips))))
        Dz = np.array(list(itertools.chain(*list(d[:,2] for d in Dips))))
        G  = np.hstack(tuple(Grads))
        GDx = np.hstack(tuple(gd[0] for gd in GDips))
        GDy = np.hstack(tuple(gd[1] for gd in GDips))
        GDz = np.hstack(tuple(gd[2] for gd in GDips))
        mE = np.array(list(itertools.chain(*list([i for pt, i in zip(Points,mEnergies) if pt in mPoints]))))
        mG = np.hstack(tuple([i for pt, i in zip(Points,mGrads) if pt in mPoints]))
        NMol = 216 # Number of molecules

        Rho_calc = OrderedDict([])
        Rho_grad = OrderedDict([])
        Rho_std  = OrderedDict([])
        Hvap_calc = OrderedDict([])
        Hvap_grad = OrderedDict([])
        Hvap_std  = OrderedDict([])
        Alpha_calc = OrderedDict([])
        Alpha_grad = OrderedDict([])
        Alpha_std  = OrderedDict([])
        Kappa_calc = OrderedDict([])
        Kappa_grad = OrderedDict([])
        Kappa_std  = OrderedDict([])
        Cp_calc = OrderedDict([])
        Cp_grad = OrderedDict([])
        Cp_std  = OrderedDict([])
        Eps0_calc = OrderedDict([])
        Eps0_grad = OrderedDict([])
        Eps0_std  = OrderedDict([])

        # The unit that converts atmospheres * nm**3 into kj/mol :)
        pvkj=0.061019351687175

        BSims = len(BPoints)
        Shots = len(Energies[0])
        N_k = np.ones(BSims)*Shots
        # Use the value of the energy for snapshot t from simulation k at potential m
        U_kln = np.zeros([BSims,BSims,Shots], dtype = np.float64)
        for m, PT in enumerate(BPoints):
            T = PT[0]
            P = PT[1] / 1.01325 if PT[2] == 'bar' else PT[1]
            beta = 1. / (kb * T)
            for k in range(BSims):
                # The correct Boltzmann factors include PV.
                # Note that because the Boltzmann factors are computed from the conditions at simulation "m",
                # the pV terms must be rescaled to the pressure at simulation "m".
                kk = Points.index(BPoints[k])
                U_kln[k, m, :]   = Energies[kk] + P*Vols[kk]*pvkj
                U_kln[k, m, :]  *= beta
        if len(BPoints) > 1:
            print "Running MBAR analysis on %i states..." % len(BPoints)
            mbar = pymbar.MBAR(U_kln, N_k, verbose=True, relative_tolerance=5.0e-8)
            W1 = mbar.getWeights()
            print "Done"
        elif len(BPoints) == 1:
            W1 = np.ones((BPoints*Shots,BPoints),dtype=float)
            W1 /= BPoints*Shots
        
        W2 = np.zeros([len(Points)*Shots,len(Points)],dtype=np.float64)
        for m, PT in enumerate(Points):
            if PT in BPoints:
                mm = BPoints.index(PT)
                for kk, PT1 in enumerate(BPoints):
                    k = Points.index(PT1)
                    # print "Will fill W2[%i:%i,%i] with W1[%i:%i,%i]" % (k*Shots,k*Shots+Shots,m,kk*Shots,kk*Shots+Shots,mm)
                    W2[k*Shots:k*Shots+Shots,m] = W1[kk*Shots:kk*Shots+Shots,mm]
            else:
                # print "Will fill W2[%i:%i,%i] with equal weights" % (m*Shots,m*Shots+Shots,m)
                W2[m*Shots:m*Shots+Shots,m] = 1.0/Shots

        # Run MBAR on the monomers.  This is barely necessary.
        mSims = len(mPoints)
        mShots = len(mEnergies[0])
        mN_k = np.ones(mSims)*mShots
        mU_kln = np.zeros([mSims,mSims,mShots], dtype = np.float64)
        for m, PT in enumerate(mPoints):
            T = PT[0]
            beta = 1. / (kb * T)
            for k in range(mSims):
                mU_kln[k, m, :]  = mEnergies[k]
                mU_kln[k, m, :] *= beta
        if np.abs(np.std(mEnergies)) > 1e-6 and mSims > 1:
            mmbar = pymbar.MBAR(mU_kln, mN_k, verbose=False, relative_tolerance=5.0e-8, method='self-consistent-iteration')
            mW1 = mmbar.getWeights()
        else:
            mW1 = np.ones((mSims*mShots,mSims),dtype=float)
            mW1 /= mSims*mShots

        for i, PT in enumerate(Points):
            T = PT[0]
            P = PT[1] / 1.01325 if PT[2] == 'bar' else PT[1]
            PV = P*V*pvkj
            H = E + PV
            # The weights that we want are the last ones.
            W = flat(W2[:,i])
            C = weight_info(W, PT, np.ones(len(Points), dtype=np.float64)*Shots, verbose=False)
            Gbar = flat(np.mat(G)*col(W))
            mBeta = -1/kb/T
            Beta  = 1/kb/T
            kT    = kb*T
            # Define some things to make the analytic derivatives easier.
            def avg(vec):
                return np.dot(W,vec)
            def covde(vec):
                return flat(np.mat(G)*col(W*vec)) - avg(vec)*Gbar
            ## Density.
            Rho_calc[PT]   = np.dot(W,R)
            Rho_grad[PT]   = mBeta*(flat(np.mat(G)*col(W*R)) - np.dot(W,R)*Gbar)
            ## Enthalpy of vaporization.
            if PT in mPoints:
                ii = mPoints.index(PT)
                mW = flat(mW1[:,ii])
                mGbar = flat(np.mat(mG)*col(mW))
                Hvap_calc[PT]  = np.dot(mW,mE) - np.dot(W,E)/NMol + kb*T - np.dot(W, PV)/NMol
                Hvap_grad[PT]  = mGbar + mBeta*(flat(np.mat(mG)*col(mW*mE)) - np.dot(mW,mE)*mGbar)
                Hvap_grad[PT] -= (Gbar + mBeta*(flat(np.mat(G)*col(W*E)) - np.dot(W,E)*Gbar)) / NMol
                Hvap_grad[PT] -= (mBeta*(flat(np.mat(G)*col(W*PV)) - np.dot(W,PV)*Gbar)) / NMol
                if hasattr(self,'use_cni') and self.use_cni:
                    print "Adding % .3f to enthalpy of vaporization at" % self.RefData['cni'][PT], PT
                    Hvap_calc[PT] += self.RefData['cni'][PT]
                if hasattr(self,'use_cvib_intra') and self.use_cvib_intra:
                    print "Adding % .3f to enthalpy of vaporization at" % self.RefData['cvib_intra'][PT], PT
                    Hvap_calc[PT] += self.RefData['cvib_intra'][PT]
                if hasattr(self,'use_cvib_inter') and self.use_cvib_inter:
                    print "Adding % .3f to enthalpy of vaporization at" % self.RefData['cvib_inter'][PT], PT
                    Hvap_calc[PT] += self.RefData['cvib_inter'][PT]
            else:
                Hvap_calc[PT]  = 0.0
                Hvap_grad[PT]  = np.zeros(self.FF.np,dtype=float)
            ## Thermal expansion coefficient.
            Alpha_calc[PT] = 1e4 * (avg(H*V)-avg(H)*avg(V))/avg(V)/(kT*T)
            GAlpha1 = mBeta * covde(H*V) / avg(V)
            GAlpha2 = Beta * avg(H*V) * covde(V) / avg(V)**2
            GAlpha3 = flat(np.mat(G)*col(W*V))/avg(V) - Gbar
            GAlpha4 = Beta * covde(H)
            Alpha_grad[PT] = 1e4 * (GAlpha1 + GAlpha2 + GAlpha3 + GAlpha4)/(kT*T)
            ## Isothermal compressibility.
            bar_unit = 0.06022141793 * 1e6
            Kappa_calc[PT] = bar_unit / kT * (avg(V**2)-avg(V)**2)/avg(V)
            GKappa1 = -1 * Beta**2 * avg(V) * covde(V**2) / avg(V)**2
            GKappa2 = +1 * Beta**2 * avg(V**2) * covde(V) / avg(V)**2
            GKappa3 = +1 * Beta**2 * covde(V)
            Kappa_grad[PT] = bar_unit*(GKappa1 + GKappa2 + GKappa3)
            ## Isobaric heat capacity.
            Cp_calc[PT] = 1000/(4.184*NMol*kT*T) * (avg(H**2) - avg(H)**2)
            if hasattr(self,'use_cvib_intra') and self.use_cvib_intra:
                print "Adding", self.RefData['devib_intra'][PT], "to the heat capacity"
                Cp_calc[PT] += self.RefData['devib_intra'][PT]
            if hasattr(self,'use_cvib_inter') and self.use_cvib_inter:
                print "Adding", self.RefData['devib_inter'][PT], "to the heat capacity"
                Cp_calc[PT] += self.RefData['devib_inter'][PT]
            GCp1 = 2*covde(H) * 1000 / 4.184 / (NMol*kT*T)
            GCp2 = mBeta*covde(H**2) * 1000 / 4.184 / (NMol*kT*T)
            GCp3 = 2*Beta*avg(H)*covde(H) * 1000 / 4.184 / (NMol*kT*T)
            Cp_grad[PT] = GCp1 + GCp2 + GCp3
            ## Static dielectric constant.
            prefactor = 30.348705333964077
            D2 = avg(Dx**2)+avg(Dy**2)+avg(Dz**2)-avg(Dx)**2-avg(Dy)**2-avg(Dz)**2
            Eps0_calc[PT] = prefactor*(D2/avg(V))/T
            GD2  = 2*(flat(np.mat(GDx)*col(W*Dx)) - avg(Dx)*flat(np.mat(GDx)*col(W))) - Beta*(covde(Dx**2) - 2*avg(Dx)*covde(Dx))
            GD2 += 2*(flat(np.mat(GDy)*col(W*Dy)) - avg(Dy)*flat(np.mat(GDy)*col(W))) - Beta*(covde(Dy**2) - 2*avg(Dy)*covde(Dy))
            GD2 += 2*(flat(np.mat(GDz)*col(W*Dz)) - avg(Dz)*flat(np.mat(GDz)*col(W))) - Beta*(covde(Dz**2) - 2*avg(Dz)*covde(Dz))
            Eps0_grad[PT] = prefactor*(GD2/avg(V) - mBeta*covde(V)*D2/avg(V)**2)/T
            ## Estimation of errors.
            Rho_std[PT]    = np.sqrt(sum(C**2 * np.array(Rho_errs)**2))
            if PT in mPoints:
                Hvap_std[PT]   = np.sqrt(sum(C**2 * np.array(Hvap_errs)**2))
            else:
                Hvap_std[PT]   = 0.0
            Alpha_std[PT]   = np.sqrt(sum(C**2 * np.array(Alpha_errs)**2)) * 1e4
            Kappa_std[PT]   = np.sqrt(sum(C**2 * np.array(Kappa_errs)**2)) * 1e6
            Cp_std[PT]   = np.sqrt(sum(C**2 * np.array(Cp_errs)**2))
            Eps0_std[PT]   = np.sqrt(sum(C**2 * np.array(Eps0_errs)**2))

        # Get contributions to the objective function
        X_Rho, G_Rho, H_Rho, RhoPrint = self.objective_term(Points, 'rho', Rho_calc, Rho_std, Rho_grad, name="Density")
        X_Hvap, G_Hvap, H_Hvap, HvapPrint = self.objective_term(Points, 'hvap', Hvap_calc, Hvap_std, Hvap_grad, name="H_vap", SubAverage=self.hvap_subaverage)
        X_Alpha, G_Alpha, H_Alpha, AlphaPrint = self.objective_term(Points, 'alpha', Alpha_calc, Alpha_std, Alpha_grad, name="Thermal Expansion")
        X_Kappa, G_Kappa, H_Kappa, KappaPrint = self.objective_term(Points, 'kappa', Kappa_calc, Kappa_std, Kappa_grad, name="Compressibility")
        X_Cp, G_Cp, H_Cp, CpPrint = self.objective_term(Points, 'cp', Cp_calc, Cp_std, Cp_grad, name="Heat Capacity")
        X_Eps0, G_Eps0, H_Eps0, Eps0Print = self.objective_term(Points, 'eps0', Eps0_calc, Eps0_std, Eps0_grad, name="Dielectric Constant")

        Gradient = np.zeros(self.FF.np, dtype=float)
        Hessian = np.zeros((self.FF.np,self.FF.np),dtype=float)

        if X_Rho == 0: self.w_rho = 0.0
        if X_Hvap == 0: self.w_hvap = 0.0
        if X_Alpha == 0: self.w_alpha = 0.0
        if X_Kappa == 0: self.w_kappa = 0.0
        if X_Cp == 0: self.w_cp = 0.0
        if X_Eps0 == 0: self.w_eps0 = 0.0

        w_tot = self.w_rho + self.w_hvap + self.w_alpha + self.w_kappa + self.w_cp + self.w_eps0
        w_1 = self.w_rho / w_tot
        w_2 = self.w_hvap / w_tot
        w_3 = self.w_alpha / w_tot
        w_4 = self.w_kappa / w_tot
        w_5 = self.w_cp / w_tot
        w_6 = self.w_eps0 / w_tot

        Objective    = w_1 * X_Rho + w_2 * X_Hvap + w_3 * X_Alpha + w_4 * X_Kappa + w_5 * X_Cp + w_6 * X_Eps0
        if AGrad:
            Gradient = w_1 * G_Rho + w_2 * G_Hvap + w_3 * G_Alpha + w_4 * G_Kappa + w_5 * G_Cp + w_6 * G_Eps0
        if AHess:
            Hessian  = w_1 * H_Rho + w_2 * H_Hvap + w_3 * H_Alpha + w_4 * H_Kappa + w_5 * H_Cp + w_6 * H_Eps0

        PrintDict = OrderedDict()
        if X_Rho > 0:
            Title = "Condensed Phase Properties:\n %-20s %40s" % ("Property Name", "Residual x Weight = Contribution")
            printcool_dictionary(RhoPrint, title='Density (kg m^-3) \nTemperature  Pressure  Reference  Calculated +- Stdev     Delta    Weight    Term   ',bold=True,color=3,keywidth=15)
            bar = printcool("Density objective function: % .3f, Derivative:" % X_Rho)
            self.FF.print_map(vals=G_Rho)
            print bar
            PrintDict['Density'] = "% 10.5f % 8.3f % 14.5e" % (X_Rho, w_1, X_Rho*w_1)

        if X_Hvap > 0:
            printcool_dictionary(HvapPrint, title='Enthalpy of Vaporization (kJ mol^-1) \nTemperature  Pressure  Reference  Calculated +- Stdev     Delta    Weight    Term   ',bold=True,color=3,keywidth=15)
            bar = printcool("H_vap objective function: % .3f, Derivative:" % X_Hvap)
            self.FF.print_map(vals=G_Hvap)
            print bar
            PrintDict['Enthalpy of Vaporization'] = "% 10.5f % 8.3f % 14.5e" % (X_Hvap, w_2, X_Hvap*w_2)

        if X_Alpha > 0:
            printcool_dictionary(AlphaPrint,title='Thermal Expansion Coefficient (10^-4 K^-1) \nTemperature  Pressure  Reference  Calculated +- Stdev     Delta    Weight    Term   ',bold=True,color=3,keywidth=15)
            bar = printcool("Thermal Expansion objective function: % .3f, Derivative:" % X_Alpha)
            self.FF.print_map(vals=G_Alpha)
            print bar
            PrintDict['Thermal Expansion Coefficient'] = "% 10.5f % 8.3f % 14.5e" % (X_Alpha, w_3, X_Alpha*w_3)

        if X_Kappa > 0:
            printcool_dictionary(KappaPrint,title='Isothermal Compressibility (10^-6 bar^-1) \nTemperature  Pressure  Reference  Calculated +- Stdev     Delta    Weight    Term   ',bold=True,color=3,keywidth=15)
            bar = printcool("Compressibility objective function: % .3f, Derivative:" % X_Kappa)
            self.FF.print_map(vals=G_Kappa)
            print bar
            PrintDict['Isothermal Compressibility'] = "% 10.5f % 8.3f % 14.5e" % (X_Kappa, w_4, X_Kappa*w_4)

        if X_Cp > 0:
            printcool_dictionary(CpPrint,   title='Isobaric Heat Capacity (cal mol^-1 K^-1) \nTemperature  Pressure  Reference  Calculated +- Stdev     Delta    Weight    Term   ',bold=True,color=3,keywidth=15)
            bar = printcool("Heat Capacity objective function: % .3f, Derivative:" % X_Cp)
            self.FF.print_map(vals=G_Cp)
            print bar
            PrintDict['Isobaric Heat Capacity'] = "% 10.5f % 8.3f % 14.5e" % (X_Cp, w_5, X_Cp*w_5)

        if X_Eps0 > 0:
            printcool_dictionary(Eps0Print,   title='Dielectric Constant\nTemperature  Pressure  Reference  Calculated +- Stdev     Delta    Weight    Term   ',bold=True,color=3,keywidth=15)
            bar = printcool("Dielectric Constant objective function: % .3f, Derivative:" % X_Eps0)
            self.FF.print_map(vals=G_Eps0)
            print bar
            PrintDict['Dielectric Constant'] = "% 10.5f % 8.3f % 14.5e" % (X_Eps0, w_6, X_Eps0*w_6)

        PrintDict['Total'] = "% 10s % 8s % 14.5e" % ("","",Objective)

        printcool_dictionary(PrintDict,color=4,title=Title,keywidth=31)

        Answer = {'X':Objective, 'G':Gradient, 'H':Hessian}
        return Answer

