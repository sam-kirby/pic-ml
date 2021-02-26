import happi
import logging
import numpy as np
import os
import shutil
import sys
import tempfile
import time

from collections.abc import Callable
from mpi4py import MPI
from multiprocessing.pool import ThreadPool
from pathlib import Path
from scipy.optimize import differential_evolution

def smilei_sim(par_vec: list[float], namelist: str, post_process: Callable[[str], float]) -> float:
    logger = logging.getLogger("supervisor")

    logger.debug(f"Starting Smilei simulation with parameters: {', '.join([str(x) for x in par_vec])}")

    # create temporary work directory - have to do it this way as directory can fail to delete on HPC...
    work_dir = tempfile.mkdtemp(dir=os.getcwd())

    # set working directory for child process
    info = MPI.Info.Create()
    info.Set("wdir", work_dir)

    # spawn smilei child process
    inter = MPI.COMM_SELF.Spawn(
        command = "smilei_sub",
        args=[
            "from numpy import array",
            "x = {}".format(par_vec.__repr__().replace("\n", "")),
            namelist
        ],
        maxprocs=1,
        info=info
    )

    # wait for smilei to finish (yielding to other threads)
    req = inter.irecv(source=0, tag=0)
    while not req.Test():
        time.sleep(5)
    req.Wait()  # this allows the child process to finish (actually do the send)

    # perform post-processing
    result = post_process(work_dir)

    logger.debug(f"Smilei Simulation finished, got result: {result}, parameters: {', '.join([str(x) for x in par_vec])}")

    # write processed result to current gen file
    with open(f"gen{get_gen_number():0>3d}.csv", 'a') as gen_file:
        xs = ','.join([str(x) for x in par_vec])
        print(xs, -result, sep=',', file=gen_file)

    # tidy up
    shutil.rmtree(work_dir, onerror=lambda _func, path, excinfo: logger.warning(f"Failed to delete {path} as {excinfo}"))

    # finally, return result
    return result

def max_energy_negated(work_dir: str) -> float:
    logger = logging.getLogger("supervisor")
    sim_results = happi.Open(work_dir)
    final_energy_spectrum = sim_results.ParticleBinning(
        0,
        timesteps=sim_results.namelist.Main.number_of_timesteps
    )
    last_occupied_energy_bin = np.nonzero(final_energy_spectrum.getData()[0])[0][-1]
    if last_occupied_energy_bin == len(final_energy_spectrum.getData()[0]) - 1:
        logger.warning("Final energy bin not empty, data loss may have occurred")
    return -final_energy_spectrum._centers[0][last_occupied_energy_bin]

def callback(xk, convergence, *args, **kwargs) -> bool:
    logger = logging.getLogger("supervisor")
    gen_number = get_gen_number()
    logger.info(f"Generation {gen_number} complete; current best density profile is: {', '.join([str(x) for x in xk])}")
    logger.info(f"Convergence is {convergence}")
    Path(f"gen{gen_number + 1:0>3d}.csv").touch()
    return False

def get_gen_number() -> int:
    return list(
        sorted(
            map(
                lambda f: int(f.name[3:6]),
                filter(
                    lambda f: f.is_file() and "gen" in f.name,
                    os.scandir()
                )
            )
        )
    )[-1]

# setup logging
logger = logging.getLogger("supervisor")
logger.setLevel(logging.DEBUG)
fh = logging.FileHandler("main.log", mode='w')
fh.setFormatter(logging.Formatter("%(levelname)s@%(asctime)s:%(message)s"))
fh.setLevel(logging.INFO)
logger.addHandler(fh)
fh = logging.FileHandler("debug.log", mode='w')
fh.setFormatter(logging.Formatter("%(levelname)s@%(asctime)s:%(message)s"))
logger.addHandler(fh)

# MPI Setup
comm = MPI.COMM_WORLD
rank = comm.Get_rank()

if (usize := comm.Get_attr(MPI.UNIVERSE_SIZE)) is None:
    logger.warning("Unable to determine universe size automatically, make sure this has been set correctly")
    usize = 64

if rank == 0:
    logger.info(f"Supervisor is running at rank {rank}. The universe contains {usize} nodes.")
else:
    logger.error(f"Supervisor expected to be running at rank 0, but is actually rank {rank}")
    sys.exit(1)

# GE Params
bounds = [(0., 3.) for _i in range(10)]
args = [f"{os.environ['HOME']}/namelists/density_ml.py", max_energy_negated]
max_iter = 100
pop_size = 12  # MULTIPLIER! len(x) * pop_size = actual_pop_size
workers = ThreadPool(processes=usize - 1).map

# Create gen0 out file
Path("gen000.csv").touch()

# GE
logger.info("Beginning optimisation")
res = differential_evolution(smilei_sim, bounds, args=args, callback=callback, maxiter=max_iter, popsize=pop_size, workers=workers, updating='deferred', polish=False)
logger.info(f"Optimizer terminated due to: {res.message}")
logger.info(f"Optimal density profile is: [{', '.join([str(x) for x in res.x])}] with a peak energy of {res.fun} sim units")