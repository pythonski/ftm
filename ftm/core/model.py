import numpy as np
import inspect
import matplotlib.pyplot as plt
import pandas as pd
import math
import matplotlib.ticker as _mticker
import os

from . import utils
from .utils import get_option, get_parameter_table, init_cli_arguments, handle_cli_arguments

# TODO Temporary really hacky way to handle the state (as a middle step in the transition to the final code)
class StateDef:
  pass

class VarDef:
  def __init__(self, shape, dtype = float):
    self.shape = shape
    self.dtype = dtype

class SimulateTakeOff():
  """ Class to run a simulation of how automation and the economy
      will feed into each other.

      The basic idea:
        - We update the SotA in hardware and software based on last years' rnd production
        - We update production factors given reinvesment: capital, labour and compute
        - We allocate those between AI training, production, hardware RnD and software RnD
        - We check which tasks can be automated given the training budget
        - We use a CES production function to estimate goods production
        - We use a CES production function to estimate rnd production
  """
  def __init__(self,

      # Automation thesholds
      full_automation_requirements_training,
      flop_gap_training,
      goods_vs_rnd_requirements_training,

      full_automation_requirements_runtime,
      flop_gap_runtime,
      goods_vs_rnd_requirements_runtime,

      runtime_training_tradeoff,
      runtime_training_max_tradeoff,

      # Production
      labour_substitution_goods,
      labour_substitution_rnd,
      capital_substitution_goods,
      capital_substitution_rnd,
      research_experiments_substitution_software,
      compute_software_rnd_experiments_efficiency,

      # R&D parameters
      hardware_returns,
      software_returns,
      hardware_performance_ceiling,
      software_ceiling,
      rnd_parallelization_penalty,
      hardware_delay,

      # Fractional inputs
      frac_capital_hardware_rnd_growth,
      frac_labour_hardware_rnd_growth,
      frac_compute_hardware_rnd_growth,

      frac_labour_software_rnd_growth,
      frac_compute_software_rnd_growth,

      frac_gwp_compute_growth,
      frac_compute_training_growth,

      frac_capital_hardware_rnd_growth_rampup,
      frac_labour_hardware_rnd_growth_rampup,
      frac_compute_hardware_rnd_growth_rampup,

      frac_labour_software_rnd_growth_rampup,
      frac_compute_software_rnd_growth_rampup,

      frac_gwp_compute_growth_rampup,
      frac_compute_training_growth_rampup,

      frac_capital_hardware_rnd_ceiling,
      frac_labour_hardware_rnd_ceiling,
      frac_compute_hardware_rnd_ceiling,

      frac_labour_software_rnd_ceiling,
      frac_compute_software_rnd_ceiling,

      frac_gwp_compute_ceiling,
      frac_compute_training_ceiling,

      # Initial values
      initial_frac_capital_hardware_rnd,
      initial_frac_labour_hardware_rnd,
      initial_frac_compute_hardware_rnd,

      initial_frac_labour_software_rnd,
      initial_frac_compute_software_rnd,

      initial_biggest_training_run,
      ratio_initial_to_cumulative_input_hardware_rnd,
      ratio_initial_to_cumulative_input_software_rnd,
      initial_hardware_production,
      ratio_hardware_to_initial_hardware_production,
      initial_buyable_hardware_performance,
      initial_gwp,
      initial_population,
      
      initial_cognitive_share_goods,
      initial_cognitive_share_hardware_rnd,
      initial_experiment_share_software_rnd,
      initial_compute_share_goods,
      initial_compute_share_rnd,

      # Other
      rampup_trigger,
      initial_capital_growth,
      labour_growth,
      tfp_growth,
      compute_depreciation,
      money_cap_training_before_wakeup,

      # Gap steepness (each how many OOM the requirements distribution jumps)
      training_requirements_steepness = 0,
      runtime_requirements_steepness = 0,

      # Simulation resolution
      t_start = None,
      t_end = None,
      t_step = None, # Time between steps in years

      dynamic_t_end = False,
      t_end_max = None, # If dynamic_t_end is True, the simulation won't continue past this year
      t_end_min = None, # If dynamic_t_end is True, the simulation won't stop before this year

      compute_shares = True,

      disable_automation = None,

      # Metadata
      title = None,

      n_labour_tasks = 100,

      # Metadata / feature flags
      runtime_training_tradeoff_enabled = True,
      rampup_enabled = True,

      # Fractional inputs cooldown growth rates
      frac_capital_hardware_rnd_growth_cooldown = 0,
      frac_labour_hardware_rnd_growth_cooldown = 0,
      frac_compute_hardware_rnd_growth_cooldown = 0,

      frac_labour_software_rnd_growth_cooldown = 0,
      frac_compute_software_rnd_growth_cooldown = 0,

      frac_gwp_compute_growth_cooldown = -0.05,
      frac_compute_training_growth_cooldown = -0.25,

      # Cool-down trigger parameters
      cooldown_threshold = 0.25,
      cooldown_window = 2,
      cooldown_enabled = True,
      ):

    if t_start is None: t_start = get_option('t_start', 2022)
    if t_end   is None: t_end   = get_option('t_end',   2100)
    if t_step  is None: t_step  = get_option('t_step',  0.1)

    if disable_automation is None: disable_automation = get_option('disable_automation', False)

    if dynamic_t_end is None: dynamic_t_end = get_option('dynamic_t_end', False)

    if dynamic_t_end:
      # We'll compute it at the end of the simulation
      t_end = None

    # Add all inputs to model parameters
    for item in inspect.signature(SimulateTakeOff).parameters:
      setattr(self, item, eval(item))

    # Checks
    self.check_input_validity()

    # Process input parameters
    self.process_input_parameters()

    # Allocate memory for the simulation state
    self.create_simulation_state()

  def check_input_validity(self):
    assert self.flop_gap_training >= 1
    assert self.flop_gap_runtime >= 1

    assert self.hardware_delay >= 0

    assert self.dynamic_t_end or self.t_start < self.t_end
    assert self.t_step > 0

    assert type(self.n_labour_tasks) is int, "n_labour_tasks must be an integer"
    assert self.n_labour_tasks > 0, "n_labour_tasks must be positive"

    # Check that the ceilings are compatible
    assert self.frac_labour_hardware_rnd_ceiling + self.frac_labour_software_rnd_ceiling < 1
    assert self.frac_compute_hardware_rnd_ceiling + self.frac_compute_software_rnd_ceiling \
         + self.frac_compute_training_ceiling < 1

  def process_input_parameters(self):
    # Labour tasks
    self.n_labour_tasks_goods = self.n_labour_tasks
    self.n_labour_tasks_rnd = self.n_labour_tasks

    # Process training and runtime flops
    self.process_automation_costs()

    # Define initial quantities
    self.initial_software = 1
    self.initial_tfp_goods = 1
    self.initial_tfp_rnd = 1
    self.investment_rate = 0.2
    self.initial_hardware = \
      self.initial_hardware_production \
      * self.ratio_hardware_to_initial_hardware_production

    # Set the initial R&D inputs to roughly match their real values
    # (note that the simulation doesn't care about these values; we could set
    # both of these to 1 and there would be no changes in the dynamics of the model)
    frac_gwp_hardware_rnd_2020 = 0.2e-2
    frac_gwp_software_rnd_2020 = 0.02e-2
    self.initial_rnd_input_hardware = self.initial_gwp * frac_gwp_hardware_rnd_2020
    self.initial_rnd_input_software = self.initial_gwp * frac_gwp_software_rnd_2020

    # Economy shares
    self.initial_capital_share_goods = 1 - self.initial_cognitive_share_goods
    self.initial_capital_share_hardware_rnd = 1 - self.initial_cognitive_share_hardware_rnd
    self.initial_cognitive_share_software_rnd = 1 - self.initial_experiment_share_software_rnd

    self.initial_compute_share_goods = \
      self.initial_compute_share_goods \
      * self.initial_cognitive_share_goods # TERRIBLE HACK
    self.initial_compute_share_hardware_rnd = \
      self.initial_compute_share_rnd \
      * self.initial_cognitive_share_hardware_rnd # TERRIBLE HACK
    self.initial_compute_share_software_rnd = \
      self.initial_compute_share_rnd \
      * self.initial_cognitive_share_software_rnd # TERRIBLE HACK

    self.initial_labour_share_goods = \
      self.initial_cognitive_share_goods - self.initial_compute_share_goods
    self.initial_labour_share_hardware_rnd = \
      self.initial_cognitive_share_hardware_rnd - self.initial_compute_share_hardware_rnd
    self.initial_labour_share_software_rnd = \
      self.initial_cognitive_share_software_rnd - self.initial_compute_share_software_rnd


    # Returns to hardware and software need to be adjusted
    # by the paralellization penalty
    self.hardware_returns = \
      self.hardware_returns / self.rnd_parallelization_penalty
    self.software_returns = \
      self.software_returns / self.rnd_parallelization_penalty

    # To make the parallel penalty equivalent to the original model we need
    # to adjust the outer R&D substitution parameter
    self.capital_substitution_rnd *= self.rnd_parallelization_penalty

    # Hardware delay is adjusted by timestep
    self.hardware_delay_idx = round(self.hardware_delay / self.t_step)

    # Deactivate runtime training tradeoff
    if self.runtime_training_tradeoff <= 0 or np.isnan(self.runtime_training_tradeoff):
      self.runtime_training_tradeoff = None
      self.runtime_training_max_tradeoff = None

  def process_automation_costs(self):
    """ Initialize the training and runtime flops for goods and rnd
    """

    seconds_per_year = 60*60*24*365

    # Define requirements and gap for goods and R&D
    self.full_automation_training_flops_goods = self.full_automation_requirements_training
    self.full_automation_runtime_flops_goods = self.full_automation_requirements_runtime * seconds_per_year

    self.full_automation_training_flops_rnd = \
      self.full_automation_requirements_training / self.goods_vs_rnd_requirements_training
    self.full_automation_runtime_flops_rnd = \
      self.full_automation_requirements_runtime * seconds_per_year / self.goods_vs_rnd_requirements_runtime

    self.automation_training_flop_gap_goods = self.flop_gap_training
    self.automation_training_flop_gap_rnd = self.flop_gap_training

    self.automation_runtime_flop_gap_goods = self.flop_gap_runtime
    self.automation_runtime_flop_gap_rnd = self.flop_gap_runtime

    # Define distribution of requirements
    self.automation_training_flops_goods = \
      SimulateTakeOff.quantiles_from_gap(
          self.full_automation_training_flops_goods,
          self.automation_training_flop_gap_goods,
          )
    self.automation_runtime_flops_goods = \
      SimulateTakeOff.quantiles_from_gap(
          self.full_automation_runtime_flops_goods,
          self.automation_runtime_flop_gap_goods,
          )
    self.automation_training_flops_rnd = \
      SimulateTakeOff.quantiles_from_gap(
          self.full_automation_training_flops_rnd,
          self.automation_training_flop_gap_rnd,
          )
    self.automation_runtime_flops_rnd = \
      SimulateTakeOff.quantiles_from_gap(
          self.full_automation_runtime_flops_rnd,
          self.automation_runtime_flop_gap_rnd,
          )

    self.automation_training_flops_goods =\
      SimulateTakeOff.process_quantiles(self.automation_training_flops_goods,
                                        self.n_labour_tasks_goods)

    self.automation_runtime_flops_goods =\
      SimulateTakeOff.process_quantiles(self.automation_runtime_flops_goods,
                                        self.n_labour_tasks_goods)

    self.automation_training_flops_rnd =\
      SimulateTakeOff.process_quantiles(self.automation_training_flops_rnd,
                                        self.n_labour_tasks_rnd)

    self.automation_runtime_flops_rnd =\
      SimulateTakeOff.process_quantiles(self.automation_runtime_flops_rnd,
                                        self.n_labour_tasks_rnd)

    self.automation_training_flops_goods =\
      SimulateTakeOff.add_steepness(
          self.full_automation_training_flops_goods,
          self.automation_training_flop_gap_goods,
          self.automation_training_flops_goods,
          self.training_requirements_steepness)

    self.automation_runtime_flops_goods =\
      SimulateTakeOff.add_steepness(
          self.full_automation_runtime_flops_goods,
          self.automation_runtime_flop_gap_goods,
          self.automation_runtime_flops_goods,
          self.runtime_requirements_steepness)

    self.automation_training_flops_rnd =\
      SimulateTakeOff.add_steepness(
          self.full_automation_training_flops_rnd,
          self.automation_training_flop_gap_rnd,
          self.automation_training_flops_rnd,
          self.training_requirements_steepness)

    self.automation_runtime_flops_rnd =\
      SimulateTakeOff.add_steepness(
          self.full_automation_runtime_flops_rnd,
          self.automation_runtime_flop_gap_rnd,
          self.automation_runtime_flops_rnd,
          self.runtime_requirements_steepness)

    # The first task is always automatable
    self.automation_training_flops_goods = \
      np.insert(self.automation_training_flops_goods, 0, 1.0)
    self.automation_runtime_flops_goods = \
      np.insert(self.automation_runtime_flops_goods, 0, 1.0)
    self.automation_training_flops_rnd = \
      np.insert(self.automation_training_flops_rnd, 0, 1.0)
    self.automation_runtime_flops_rnd = \
      np.insert(self.automation_runtime_flops_rnd, 0, 1.0)

    # Check that the automation costs are monotonic
    if np.any(np.diff(self.automation_training_flops_goods) < 0.) \
    or np.any(np.diff(self.automation_runtime_flops_goods) < 0.) \
    or np.any(np.diff(self.automation_training_flops_rnd) < 0.) \
    or np.any(np.diff(self.automation_runtime_flops_rnd) < 0.):
      raise ValueError("Assumption not met: the automation costs must be monotonically increasing.")

  ##############################################################################

  def create_simulation_state(self):
    # Create dynamic empty vectors to hold the results of the simulation

    self.state_def = StateDef()

    self.state_def.timesteps = self.state_var()

    self.create_simulation_state_investment()
    self.create_simulation_state_automation()
    self.create_simulation_state_production()

    self.process_state()

  def create_simulation_state_investment(self):

    self.create_simulation_state_rnd()
    self.create_simulation_state_total_input()
    self.create_simulation_state_fractional_inputs()

  def create_simulation_state_rnd(self):
    # Hardware RnD
    self.state_def.cumulative_rnd_input_hardware = self.state_var()
    self.state_def.hardware_performance = self.state_var()

    # Software RnD
    self.state_def.cumulative_rnd_input_software = self.state_var()
    self.state_def.software = self.state_var()

  def create_simulation_state_fractional_inputs(self):

    self.state_def.rampup = self.state_var(dtype=bool)
    self.rampup_start = None
    self.rampup_mid = None
    self.cooldown_start = None  # first timestep entering cooldown

    # Goods vs compute investment split
    self.state_def.frac_gwp_compute = self.state_var()

    # Total R&D fractional inputs
    self.state_def.frac_capital_hardware_rnd = self.state_var()
    self.state_def.frac_labour_hardware_rnd = self.state_var()
    self.state_def.frac_compute_hardware_rnd = self.state_var()

    self.state_def.frac_labour_software_rnd = self.state_var()
    self.state_def.frac_compute_software_rnd = self.state_var()

    # Training compute
    self.state_def.frac_compute_training = self.state_var()

    # Goods production fractional inputs
    self.state_def.frac_capital_goods = self.state_var()
    self.state_def.frac_labour_goods = self.state_var()
    self.state_def.frac_compute_goods = self.state_var()

    # Cool-down phase indicator (mirrors ramp-up but for slow automation periods)
    self.state_def.cooldown = self.state_var(dtype=bool)

  def create_simulation_state_total_input(self):
    self.state_def.capital = self.state_var()
    self.state_def.labour = self.state_var()
    self.state_def.compute_investment = self.state_var()
    self.state_def.hardware = self.state_var()
    self.state_def.compute = self.state_var()

    self.state_def.tfp_goods = self.state_var()
    self.state_def.tfp_rnd = self.state_var()
    self.state_def.money_spent_training = self.state_var()

  def create_simulation_state_automation(self):
    self.state_def.biggest_training_run = self.state_var()
    self.state_def.automatable_tasks_goods_no_tradeoff = self.state_var(dtype=int)
    self.state_def.automatable_tasks_rnd_no_tradeoff = self.state_var(dtype=int)
    self.state_def.automatable_tasks_goods = self.state_var(dtype=int)
    self.state_def.automatable_tasks_rnd = self.state_var(dtype=int)
    self.state_def.frac_automatable_tasks = self.state_var()
    self.state_def.frac_automatable_tasks_goods_no_tradeoff = self.state_var()
    self.state_def.frac_automatable_tasks_rnd_no_tradeoff = self.state_var()
    self.state_def.frac_automatable_tasks_goods = self.state_var()
    self.state_def.frac_automatable_tasks_rnd = self.state_var()
    self.state_def.task_compute_to_labour_ratio_goods = self.state_var((self.n_labour_tasks_goods+1,))
    self.state_def.task_compute_to_labour_ratio_rnd = self.state_var((self.n_labour_tasks_rnd+1,))
    self.agi_year = None
    self.sub_agi_year = None

  def create_simulation_state_production(self):
    # Goods production
    self.state_def.capital_goods = self.state_var()
    self.state_def.labour_goods = self.state_var()
    self.state_def.compute_goods = self.state_var()

    self.state_def.labour_task_input_goods = self.state_var((self.n_labour_tasks_goods+1,))
    self.state_def.compute_task_input_goods = self.state_var((self.n_labour_tasks_goods+1,))
    self.state_def.task_input_goods = self.state_var((self.n_labour_tasks_goods+1,))

    self.state_def.frac_tasks_automated_goods = self.state_var()

    self.state_def.automation_multiplier_goods = self.state_var()
    self.state_def.gwp = self.state_var()

    self.state_def.capital_share_goods = self.state_var()
    self.state_def.cognitive_share_goods = self.state_var()
    self.state_def.labour_share_goods = self.state_var()
    self.state_def.compute_share_goods = self.state_var()

    # Hardware RnD production
    self.state_def.capital_hardware_rnd = self.state_var()
    self.state_def.labour_hardware_rnd = self.state_var()
    self.state_def.compute_hardware_rnd = self.state_var()

    self.state_def.labour_task_input_hardware_rnd = self.state_var((self.n_labour_tasks_rnd + 1,))
    self.state_def.compute_task_input_hardware_rnd = self.state_var((self.n_labour_tasks_rnd + 1,))
    self.state_def.task_input_hardware_rnd = self.state_var((self.n_labour_tasks_rnd + 1,))

    self.state_def.frac_tasks_automated_rnd = self.state_var()
    self.state_def.rnd_input_hardware = self.state_var()
    self.state_def.automation_multiplier_rnd = self.state_var()

    self.state_def.capital_share_hardware_rnd = self.state_var()
    self.state_def.cognitive_share_hardware_rnd = self.state_var()
    self.state_def.labour_share_hardware_rnd = self.state_var()
    self.state_def.compute_share_hardware_rnd = self.state_var()

    self.state_def.experiment_share_software_rnd = self.state_var()
    self.state_def.cognitive_share_software_rnd = self.state_var()
    self.state_def.labour_share_software_rnd = self.state_var()
    self.state_def.compute_share_software_rnd = self.state_var()

    # Software RnD production
    self.state_def.capital_software_rnd = self.state_var()
    self.state_def.labour_software_rnd = self.state_var()
    self.state_def.compute_software_rnd = self.state_var()
    self.state_def.compute_software_rnd_experiments = self.state_var()

    self.state_def.labour_task_input_software_rnd = self.state_var((self.n_labour_tasks_rnd + 1,))
    self.state_def.compute_task_input_software_rnd = self.state_var((self.n_labour_tasks_rnd + 1,))
    self.state_def.task_input_software_rnd = self.state_var((self.n_labour_tasks_rnd + 1,))

    self.state_def.rnd_input_software = self.state_var()

  def state_var(self, item_shape = (), dtype = float):
    return VarDef(shape = item_shape, dtype = dtype)

  def process_state(self):
    self.state_lists = []

    self.state_list_len = 0
    self.actual_state_list_len = int(100/self.t_step)

    for attribute, var_def in self.state_def.__dict__.items():
      if var_def.shape == ():
        l = [var_def.dtype() for i in range(self.actual_state_list_len)]
      else:
        l = [np.zeros(var_def.shape, dtype=var_def.dtype) for i in range(self.actual_state_list_len)]
      self.state_lists.append(l)

      setattr(self, attribute, l)

  def reset_state(self):
    for l in self.state_lists:
      v = l[0]
      for i in range(self.state_list_len):
        l[i] = type(v)() if np.isscalar(v) else np.zeros_like(v)
    self.state_list_len = 0

  def post_process_state(self):
    for attribute in self.state_def.__dict__.keys():
      # Convert the lists into Numpy arrays
      setattr(self, attribute, np.array(getattr(self, attribute)[:self.state_list_len]))

  def tick(self):
    # ensure we have enough space for the arrays

    self.state_list_len += 1

    while self.state_list_len > self.actual_state_list_len:
      size_to_add = self.actual_state_list_len
      self.actual_state_list_len += size_to_add

      for l in self.state_lists:
        v = l[0]
        l += [type(v)() if np.isscalar(v) else np.zeros_like(v) for x in range(size_to_add)]

  ########################################################################

  def run_simulation(self):
    # Treat NumPy's floating-point warnings as exceptions
    with np.errstate(invalid = 'raise'):
      self.reset_state()

      try:
        t_idx = 0
        while self.continue_simulation(t_idx):
          t_year = self.index_to_time(t_idx)

          self.tick()

          self.timesteps[t_idx] = t_year
          self.t_idx = t_idx
          if t_idx == 0:
            self.initialize_inputs()
          else:
            self.reinvest_output_in_inputs(t_idx)
          self.automate_tasks(t_idx)
          self.production(t_idx)

          t_idx += 1

      except FloatingPointError as e:
        self.handle_exception(e)
      finally:
        self.n_timesteps = t_idx
        self.t_end = self.index_to_time(t_idx)

    self.post_process_state()

    # Compute takeoff metrics
    self.compute_metrics()

  def continue_simulation(self, t_idx):
    t_year = self.index_to_time(t_idx)

    if not self.dynamic_t_end:
      # Stop when the caller wants us to stop
      return t_year < self.t_end
    else:
      # Stop when we can compute all metrics
      # TODO I don't like this :(

      # Give us a chance
      if t_idx == 0:
        return True

      # OK, that was enough
      if (self.t_end_max is not None and t_year >= self.t_end_max) or t_idx > 100000:
        return False

      if (self.t_end_min is not None and t_year < self.t_end_min):
        return True

      # Do we have AGI?
      if self.agi_year is None:
        return True

      # Can we already compute the cog_output_multiplier metric?
      if self.automation_multiplier_rnd[t_idx-1] <= 10:
        return True

      # Haven't we automated every task yet? (We'll extend the simulation for a year, in order to be able to compute growth rates)
      one_year_ago_idx = int(math.floor(t_idx - 1/self.t_step))
      if self.frac_tasks_automated_goods[one_year_ago_idx-1] < 1 or self.frac_tasks_automated_rnd[one_year_ago_idx-1] < 1:
        return True

      # Can't we compute the gwp_growth metric?
      if not self.disable_automation:
        delta = int(1 / self.t_step)
        gwp_growth = np.log(np.divide(self.gwp[delta:self.t_idx], self.gwp[:self.t_idx-delta]))
        if np.all(gwp_growth <= 0.05) or np.all(gwp_growth <= 0.20):
          return True

      return False

  def handle_exception(self, e):
    import traceback

    print("An overflow has happened and the simulation has stopped.")
    print(e)
    print(traceback.format_exc(), end = '')

    # Pickle the model for further inspection
    import dill as pickle
    import os

    if not hasattr(SimulateTakeOff, 'dump_count'):
      SimulateTakeOff.dump_count = 0

    cache_dir = os.path.join(get_option('cache_dir'), 'dumps')
    os.makedirs(cache_dir, exist_ok=True)
    self.exception = e
    pickle_file = os.path.join(cache_dir, f'model_{SimulateTakeOff.dump_count}.pickle')
    with open(pickle_file, 'wb') as f:
      pickle.dump(self, f)

    SimulateTakeOff.dump_count = (SimulateTakeOff.dump_count + 1) % 4 # limit the number of pickle files

    print(f"You can find a copy of the model in {pickle_file}")
    print()

  ########################################################################

  # INPUT INITIALIZATION
  def initialize_inputs(self):
    self.initialize_rnd_state()
    self.initialize_total_inputs()
    self.initialize_fractional_inputs()

  def initialize_rnd_state(self):

    ## Hardware

    self.cumulative_rnd_input_hardware[0]  =                \
      self.initial_rnd_input_hardware                       \
      ** self.rnd_parallelization_penalty                   \
      / self.ratio_initial_to_cumulative_input_hardware_rnd \
      / self.rnd_parallelization_penalty

    ## Software
    self.software[0] = self.initial_software

    self.cumulative_rnd_input_software[0]  =                \
      self.initial_rnd_input_software                       \
      ** self.rnd_parallelization_penalty                   \
      / self.ratio_initial_to_cumulative_input_software_rnd \
      / self.rnd_parallelization_penalty

  def initialize_total_inputs(self):
    self.labour[0] = self.initial_population

    # Capital is initialized so that its rate of growth in the first time step
    # matches the gwp rate of growth
    self.capital[0] =\
      self.initial_gwp * self.investment_rate \
      / (np.exp(self.initial_capital_growth)-1)

    self.hardware[0] = self.initial_hardware

    self.compute_investment[0] = \
      self.initial_hardware_production * self.t_step \
      / self.initial_buyable_hardware_performance

    self.compute[0] = self.hardware[0] * self.initial_software

    self.tfp_goods[0] = self.initial_tfp_goods
    self.tfp_rnd[0] = self.initial_tfp_rnd

    self.money_spent_training[0] = \
      self.initial_biggest_training_run / (self.initial_software * self.initial_buyable_hardware_performance)

  def initialize_fractional_inputs(self):

    self.rampup[0] = False

    # Split of gwp between capital and compute
    self.frac_gwp_compute[0] = self.compute_investment[0] / self.initial_gwp / self.t_step

    # R&D fractional inputs
    self.frac_capital_hardware_rnd[0] = self.initial_frac_capital_hardware_rnd
    self.frac_labour_hardware_rnd[0] = self.initial_frac_labour_hardware_rnd
    self.frac_compute_hardware_rnd[0] = self.initial_frac_compute_hardware_rnd

    self.frac_labour_software_rnd[0] = self.initial_frac_labour_software_rnd
    self.frac_compute_software_rnd[0] = self.initial_frac_compute_software_rnd

    # Training compute is initialized to match initial largest training run
    self.frac_compute_training[0] = self.initial_biggest_training_run / self.compute[0]

    ## Initial compute must be greater than initial training run
    if self.initial_biggest_training_run > self.compute[0]:
      raise ValueError("Initial biggest training run is bigger than available compute")

    # Goods production fractional inputs
    self.frac_capital_goods[0] = \
      1 - self.frac_capital_hardware_rnd[0]
    self.frac_labour_goods[0] = \
      1 - self.frac_labour_hardware_rnd[0] - self.frac_labour_software_rnd[0]
    self.frac_compute_goods[0] = \
      1 - self.frac_compute_hardware_rnd[0] - self.frac_compute_software_rnd[0] \
        - self.frac_compute_training[0]

    # Initialise cool-down state
    self.cooldown[0] = False

  #############################################################################

  # TASK AUTOMATION
  def automate_tasks(self, t_idx):
    # Compute largest training run
    self.biggest_training_run[t_idx] = \
      self.compute[t_idx] * self.frac_compute_training[t_idx]

    # Update index of automatable tasks
    self.automatable_tasks_goods_no_tradeoff[t_idx] = np.sum(self.automation_training_flops_goods < self.biggest_training_run[t_idx])
    self.automatable_tasks_rnd_no_tradeoff[t_idx] = np.sum(self.automation_training_flops_rnd < self.biggest_training_run[t_idx])

    self.automatable_tasks_goods[t_idx] = \
      np.sum(
          self.automation_training_flops_goods < \
          self.biggest_training_run[t_idx] \
          * (self.runtime_training_max_tradeoff \
          if self.runtime_training_tradeoff is not None \
          else 1.)
      )

    self.automatable_tasks_rnd[t_idx] = \
      np.sum(
          self.automation_training_flops_rnd < \
          self.biggest_training_run[t_idx] \
          * (self.runtime_training_max_tradeoff \
          if self.runtime_training_tradeoff is not None \
          else 1.)
      )

    # Update fraction of automated tasks
    self.frac_automatable_tasks_goods_no_tradeoff[t_idx] =  \
      (self.automatable_tasks_goods_no_tradeoff[t_idx] - 1) \
      / self.n_labour_tasks_goods # We dont count the initial compute task

    self.frac_automatable_tasks_rnd_no_tradeoff[t_idx] =  \
      (self.automatable_tasks_rnd_no_tradeoff[t_idx] - 1) \
      / self.n_labour_tasks_rnd # We dont count the initial compute task

    self.frac_automatable_tasks_goods[t_idx] =  \
      (self.automatable_tasks_goods[t_idx] - 1) \
      / self.n_labour_tasks_goods # We dont count the initial compute task

    self.frac_automatable_tasks_rnd[t_idx] =  \
      (self.automatable_tasks_rnd[t_idx] - 1) \
      / self.n_labour_tasks_rnd # We dont count the initial compute task

    self.frac_automatable_tasks[t_idx] = \
      (self.automatable_tasks_goods[t_idx] \
      + self.automatable_tasks_rnd[t_idx] - 2)  \
      / (self.n_labour_tasks_goods
         + self.n_labour_tasks_rnd) # We dont count the initial compute task

    runtime_requirements_goods = SimulateTakeOff.compute_runtime_requirements(
      self.runtime_training_tradeoff,
      self.automation_training_flops_goods,
      self.automation_runtime_flops_goods,
      self.biggest_training_run[t_idx],
    )
    self.task_compute_to_labour_ratio_goods[t_idx] = 1. / runtime_requirements_goods

    runtime_requirements_rnd = SimulateTakeOff.compute_runtime_requirements(
      self.runtime_training_tradeoff,
      self.automation_training_flops_rnd,
      self.automation_runtime_flops_rnd,
      self.biggest_training_run[t_idx],
    )
    self.task_compute_to_labour_ratio_rnd[t_idx] = 1. / runtime_requirements_rnd

  @staticmethod
  def compute_runtime_requirements(runtime_training_tradeoff, automation_training_flops, automation_runtime_flops, biggest_training_run):
    with np.errstate(under = 'ignore'):
      # Ignore underflows (we are taking care of them below with np.maximum)
      runtime_requirements = automation_runtime_flops
      if runtime_training_tradeoff is not None:
        runtime_requirements = runtime_requirements * (automation_training_flops/biggest_training_run)**runtime_training_tradeoff
    runtime_requirements = np.maximum(1., runtime_requirements)  # Requirements cannot fall below 1
    return runtime_requirements

  ##############################################################################

  # PRODUCTION
  def production(self, t_idx):
    self.goods_production(t_idx)
    self.hardware_rnd_production(t_idx)
    self.software_rnd_production(t_idx)

  def goods_production(self, t_idx):
    # Compute goods production budgets
    self.capital_goods[t_idx] = self.capital[t_idx] * self.frac_capital_goods[t_idx]
    self.labour_goods[t_idx] = self.labour[t_idx] * self.frac_labour_goods[t_idx]
    self.compute_goods[t_idx] = self.compute[t_idx] * self.frac_compute_goods[t_idx]

    # Initialize task weights to match the initial economy share ratio
    if t_idx == 0:

      no_automation_labour_task_input_goods = np.zeros(self.n_labour_tasks_goods + 1)
      no_automation_labour_task_input_goods[1:] = self.labour_goods[0] / self.n_labour_tasks_goods

      no_automation_compute_task_input_goods = np.zeros(self.n_labour_tasks_goods + 1)
      no_automation_compute_task_input_goods[0] = self.compute_goods[0]

      initial_capital_to_cognitive_share_ratio_goods = \
        self.initial_capital_share_goods / self.initial_cognitive_share_goods
      initial_compute_to_labour_share_ratio_goods = \
        self.initial_compute_share_goods / self.initial_labour_share_goods

      self.capital_task_weights_goods, \
      self.labour_task_weights_goods = \
        SimulateTakeOff.adjust_task_weights(
          self.capital_goods[0],
          no_automation_labour_task_input_goods,
          no_automation_compute_task_input_goods,
          self.task_compute_to_labour_ratio_goods[0][:],
          self.capital_substitution_goods,
          self.labour_substitution_goods,
          initial_capital_to_cognitive_share_ratio_goods,
          initial_compute_to_labour_share_ratio_goods,
        )

    # Compute optimal task allocation
    self.labour_task_input_goods[t_idx][:], \
    self.compute_task_input_goods[t_idx][:] = \
      SimulateTakeOff.solve_allocation(
          self.labour_goods[t_idx],
          self.compute_goods[t_idx],
          self.labour_task_weights_goods,
          self.labour_substitution_goods,
          self.task_compute_to_labour_ratio_goods[t_idx],
          self.automatable_tasks_goods[t_idx]
          )

    self.task_input_goods[t_idx][:] = \
      self.labour_task_input_goods[t_idx][:] + \
      self.task_compute_to_labour_ratio_goods[t_idx]*self.compute_task_input_goods[t_idx][:]

    self.frac_tasks_automated_goods[t_idx] =\
      (np.sum(self.task_compute_to_labour_ratio_goods[t_idx]*self.compute_task_input_goods[t_idx] > 10 * self.labour_task_input_goods[t_idx]) - 1) \
      / self.n_labour_tasks_goods
    ## We substract 1 to account for the initial compute task

    # Keep track of economy share ratios
    if self.compute_shares:
      self.capital_share_goods[t_idx],   \
      self.cognitive_share_goods[t_idx], \
      self.labour_share_goods[t_idx],    \
      self.compute_share_goods[t_idx] =  \
        SimulateTakeOff.compute_shares(
            self.capital_goods[t_idx],
            self.labour_task_input_goods[t_idx],
            self.compute_task_input_goods[t_idx],
            self.capital_task_weights_goods, \
            self.labour_task_weights_goods,
            self.task_compute_to_labour_ratio_goods[t_idx],
            self.capital_substitution_goods,
            self.labour_substitution_goods,
        )

    # Compute output
    output = \
      SimulateTakeOff.nested_ces_production_function(
          self.capital_goods[t_idx],
          self.task_input_goods[t_idx],
          self.capital_task_weights_goods,
          self.labour_task_weights_goods,
          self.capital_substitution_goods,
          self.labour_substitution_goods,
          self.tfp_goods[t_idx],
          )

    ## Compute how much worse is the output without automation
    # Compute optimal task allocation
    no_automation_labour_task_input_goods, \
    no_automation_compute_task_input_goods = \
      SimulateTakeOff.solve_allocation(
          self.labour_goods[t_idx],
          self.compute_goods[t_idx],
          self.labour_task_weights_goods,
          self.labour_substitution_goods,
          self.task_compute_to_labour_ratio_goods[t_idx],
          AT=1 # Only first task is automatable
          )

    no_automation_task_input_goods = \
      no_automation_labour_task_input_goods + \
      self.task_compute_to_labour_ratio_goods[t_idx]*no_automation_compute_task_input_goods

    no_automation_output = \
      SimulateTakeOff.nested_ces_production_function(
          self.capital_goods[t_idx],
          no_automation_task_input_goods,
          self.capital_task_weights_goods,
          self.labour_task_weights_goods,
          self.capital_substitution_goods,
          self.labour_substitution_goods,
          self.tfp_goods[t_idx],
          )

    self.automation_multiplier_goods[t_idx] = output / no_automation_output

    if self.disable_automation:
      output = no_automation_output

    ## Compute the ratio of output to gwp
    if t_idx == 0:
      self.output_to_gwp_factor = self.initial_gwp / output

    self.gwp[t_idx] = output * self.output_to_gwp_factor


  def hardware_rnd_production(self, t_idx):
    # Compute hardware rnd production budgets
    self.capital_hardware_rnd[t_idx] = self.capital[t_idx] * self.frac_capital_hardware_rnd[t_idx]
    self.labour_hardware_rnd[t_idx] = self.labour[t_idx] * self.frac_labour_hardware_rnd[t_idx]
    self.compute_hardware_rnd[t_idx] = self.compute[t_idx] * self.frac_compute_hardware_rnd[t_idx]

    # Initialize task weights to match the initial economy share ratio
    if t_idx == 0:
      no_automation_labour_task_input_rnd = np.zeros(self.n_labour_tasks_rnd + 1)
      no_automation_labour_task_input_rnd[1:] = self.labour_hardware_rnd[0] / self.n_labour_tasks_rnd

      no_automation_compute_task_input_rnd = np.zeros(self.n_labour_tasks_rnd + 1)
      no_automation_compute_task_input_rnd[0] = self.compute_hardware_rnd[0]

      initial_capital_to_cognitive_share_ratio_hardware_rnd = \
        self.initial_capital_share_hardware_rnd / self.initial_cognitive_share_hardware_rnd
      initial_compute_to_labour_share_ratio_hardware_rnd = \
        self.initial_compute_share_hardware_rnd / self.initial_labour_share_hardware_rnd

      self.capital_task_weights_hardware_rnd, \
      self.labour_task_weights_hardware_rnd, =\
        SimulateTakeOff.adjust_task_weights(
          self.capital_hardware_rnd[0],
          no_automation_labour_task_input_rnd,
          no_automation_compute_task_input_rnd,
          self.task_compute_to_labour_ratio_rnd[0][:],
          self.capital_substitution_rnd,
          self.labour_substitution_rnd,
          initial_capital_to_cognitive_share_ratio_hardware_rnd,
          initial_compute_to_labour_share_ratio_hardware_rnd,
        )

    # Compute optimal task allocation
    self.labour_task_input_hardware_rnd[t_idx][:], \
    self.compute_task_input_hardware_rnd[t_idx][:] = \
      SimulateTakeOff.solve_allocation(
          self.labour_hardware_rnd[t_idx],
          self.compute_hardware_rnd[t_idx],
          self.labour_task_weights_hardware_rnd,
          self.labour_substitution_rnd,
          self.task_compute_to_labour_ratio_rnd[t_idx],
          self.automatable_tasks_rnd[t_idx]
          )

    self.task_input_hardware_rnd[t_idx][:] = \
      self.labour_task_input_hardware_rnd[t_idx][:] + \
      self.task_compute_to_labour_ratio_rnd[t_idx]*self.compute_task_input_hardware_rnd[t_idx][:]

    # Note down fraction of tasks automated
    self.frac_tasks_automated_rnd[t_idx] =\
      (np.sum(self.task_compute_to_labour_ratio_rnd[t_idx]*self.compute_task_input_hardware_rnd[t_idx] > 10 * self.labour_task_input_hardware_rnd[t_idx]) - 1) \
      / self.n_labour_tasks_rnd
    ## We substract 1 to account for the initial compute task

    # Keep track of economy shares
    if self.compute_shares:
      self.capital_share_hardware_rnd[t_idx], \
      self.cognitive_share_hardware_rnd[t_idx], \
      self.labour_share_hardware_rnd[t_idx], \
      self.compute_share_hardware_rnd[t_idx] =\
        SimulateTakeOff.compute_shares(
            self.capital_hardware_rnd[t_idx],
            self.labour_task_input_hardware_rnd[t_idx],
            self.compute_task_input_hardware_rnd[t_idx],
            self.capital_task_weights_hardware_rnd, \
            self.labour_task_weights_hardware_rnd,
            self.task_compute_to_labour_ratio_rnd[t_idx],
            self.capital_substitution_rnd,
            self.labour_substitution_rnd,
        )

    # Compute output

    output_hardware = \
      SimulateTakeOff.nested_ces_production_function(
          self.capital_hardware_rnd[t_idx],
          self.task_input_hardware_rnd[t_idx],
          self.capital_task_weights_hardware_rnd,
          self.labour_task_weights_hardware_rnd,
          self.capital_substitution_rnd,
          self.labour_substitution_rnd,
          self.tfp_rnd[t_idx],
          )

    ## Compute how much worse is the hardware output without automation
    # Compute optimal task allocation
    no_automation_labour_task_input_rnd, \
    no_automation_compute_task_input_rnd = \
      SimulateTakeOff.solve_allocation(
          self.labour_hardware_rnd[t_idx],
          self.compute_hardware_rnd[t_idx],
          self.labour_task_weights_hardware_rnd,
          self.labour_substitution_rnd,
          self.task_compute_to_labour_ratio_rnd[t_idx],
          AT=1 # Only first task is automatable
          )

    no_automation_task_input_hardware_rnd = \
      no_automation_labour_task_input_rnd + \
      self.task_compute_to_labour_ratio_rnd[t_idx]*no_automation_compute_task_input_rnd

    no_automation_output = \
      SimulateTakeOff.nested_ces_production_function(
          self.capital_hardware_rnd[t_idx],
          no_automation_task_input_hardware_rnd,
          self.capital_task_weights_hardware_rnd,
          self.labour_task_weights_hardware_rnd,
          self.capital_substitution_rnd,
          self.labour_substitution_rnd,
          self.tfp_rnd[t_idx],
          )

    self.automation_multiplier_rnd[t_idx] = output_hardware / no_automation_output

    if self.disable_automation:
      output_hardware = no_automation_output

    if t_idx == 0:
      self.rnd_input_to_hardware_investment_factor = \
        self.initial_rnd_input_hardware / output_hardware

    self.rnd_input_hardware[t_idx] = \
      output_hardware * self.rnd_input_to_hardware_investment_factor

  def software_rnd_production(self, t_idx):
  
    # Compute software rnd production budgets
    self.labour_software_rnd[t_idx] = self.labour[t_idx] * self.frac_labour_software_rnd[t_idx]
    self.compute_software_rnd[t_idx] = self.compute[t_idx] * self.frac_compute_software_rnd[t_idx]
    self.compute_software_rnd_experiments[t_idx] = self.hardware[t_idx] ** self.compute_software_rnd_experiments_efficiency
    
    # Initialize task weights to match the initial economy share ratio
    if t_idx == 0:
      no_automation_labour_task_input_rnd = np.zeros(self.n_labour_tasks_rnd + 1)
      no_automation_labour_task_input_rnd[1:] = self.labour_software_rnd[0] / self.n_labour_tasks_rnd

      no_automation_compute_task_input_rnd = np.zeros(self.n_labour_tasks_rnd + 1)
      no_automation_compute_task_input_rnd[0] = self.compute_software_rnd[0]

      initial_experiment_to_cognitive_share_ratio_software_rnd = \
        self.initial_experiment_share_software_rnd / self.initial_cognitive_share_software_rnd
      initial_compute_to_labour_share_ratio_software_rnd = \
        self.initial_compute_share_software_rnd / self.initial_labour_share_software_rnd
      
      self.research_experiments_task_weights_software, \
      self.labour_task_weights_software_rnd, =\
        SimulateTakeOff.adjust_task_weights(
          self.compute_software_rnd_experiments[0],
          no_automation_labour_task_input_rnd,
          no_automation_compute_task_input_rnd,
          self.task_compute_to_labour_ratio_rnd[0][:],
          self.research_experiments_substitution_software,
          self.labour_substitution_rnd,
          initial_experiment_to_cognitive_share_ratio_software_rnd,
          initial_compute_to_labour_share_ratio_software_rnd,
        )
    
    
    # Compute optimal task allocation
    self.labour_task_input_software_rnd[t_idx][:], \
    self.compute_task_input_software_rnd[t_idx][:] = \
      SimulateTakeOff.solve_allocation(
          self.labour_software_rnd[t_idx],
          self.compute_software_rnd[t_idx],
          self.labour_task_weights_software_rnd,
          self.labour_substitution_rnd,
          self.task_compute_to_labour_ratio_rnd[t_idx],
          self.automatable_tasks_rnd[t_idx]
          )

    self.task_input_software_rnd[t_idx][:] = \
      self.labour_task_input_software_rnd[t_idx][:]  \
      + self.task_compute_to_labour_ratio_rnd[t_idx] \
      * self.compute_task_input_software_rnd[t_idx][:]
    
    # Keep track of economy shares
    if self.compute_shares:
      self.experiment_share_software_rnd[t_idx], \
      self.cognitive_share_software_rnd[t_idx], \
      self.labour_share_software_rnd[t_idx], \
      self.compute_share_software_rnd[t_idx] =\
        SimulateTakeOff.compute_shares(
            self.compute_software_rnd_experiments[t_idx],
            self.labour_task_input_software_rnd[t_idx],
            self.compute_task_input_software_rnd[t_idx],
            self.research_experiments_task_weights_software, \
            self.labour_task_weights_software_rnd,
            self.task_compute_to_labour_ratio_rnd[t_idx],
            self.research_experiments_substitution_software,
            self.labour_substitution_rnd,
        )
      
    
    # Compute research output
    research_output = \
      SimulateTakeOff.ces_production_function(
          self.task_input_software_rnd[t_idx][:],
          self.labour_task_weights_software_rnd,
          self.labour_substitution_rnd,
          )

    # Combine with experiments
    output_software = \
      SimulateTakeOff.ces_production_function(
          np.array([self.compute_software_rnd_experiments[t_idx], research_output]),
          self.research_experiments_task_weights_software,
          self.research_experiments_substitution_software,
          self.tfp_rnd[t_idx]
          )

    ## Compute how much worse is the software output without automation
    # Compute optimal task allocation
    no_automation_labour_task_input_software_rnd, \
    no_automation_compute_task_input_software_rnd = \
      SimulateTakeOff.solve_allocation(
          self.labour_software_rnd[t_idx],
          self.compute_software_rnd[t_idx],
          self.labour_task_weights_software_rnd,
          self.labour_substitution_rnd,
          self.task_compute_to_labour_ratio_rnd[t_idx],
          AT=1
          )

    no_automation_task_input_software_rnd = \
      no_automation_labour_task_input_software_rnd[:]  \
      + self.task_compute_to_labour_ratio_rnd[t_idx] \
      * no_automation_compute_task_input_software_rnd[:]

    no_automation_research_output = \
      SimulateTakeOff.ces_production_function(
          no_automation_task_input_software_rnd[:],
          self.labour_task_weights_software_rnd,
          self.labour_substitution_rnd,
          )

    # Combine with experiments
    no_automation_output = \
      SimulateTakeOff.ces_production_function(
          np.array([self.compute_software_rnd_experiments[t_idx], no_automation_research_output]),
          self.research_experiments_task_weights_software,
          self.research_experiments_substitution_software,
          self.tfp_rnd[t_idx]
          )

    if self.disable_automation:
      output_software = no_automation_output

    if t_idx == 0:
      self.rnd_input_to_software_investment_factor = \
        self.initial_rnd_input_software / output_software

    self.rnd_input_software[t_idx] =\
      output_software * self.rnd_input_to_software_investment_factor

  #############################################################################

  ## REINVEST OUTPUT IN INPUTS
  def reinvest_output_in_inputs(self, t_idx):
    self.update_rnd(t_idx)
    self.allocate_fractional_inputs(t_idx)
    self.calculate_total_inputs(t_idx)

  def update_rnd(self, t_idx):

    def _update_rnd(
        current_performance,
        initial_performance,
        research_input,
        cumulative_adjusted_input,
        returns,
        performance_ceiling
        ):
      adjusted_input =\
        research_input**self.rnd_parallelization_penalty
      new_cumulative_adjusted_input =\
        cumulative_adjusted_input + adjusted_input*self.t_step
      growth_in_cumulative_inputs =\
        new_cumulative_adjusted_input / \
        cumulative_adjusted_input
      ceiling_penalty = \
        1 \
        if performance_ceiling == np.inf \
        else \
        (np.log10(performance_ceiling) - np.log10(current_performance)) /\
        (np.log10(performance_ceiling) - np.log10(initial_performance))
      performance_growth_rate = \
        growth_in_cumulative_inputs**(returns * ceiling_penalty)
      new_performance = \
        min(
            current_performance * performance_growth_rate,
            performance_ceiling
        )

      return new_performance, new_cumulative_adjusted_input

    # Hardware

    # In the first time step, we move forward the buyable hardware
    # performance to adjust for the delay in hardware performance
    if t_idx == 1:
      improved_hardware_performance, _ = \
        _update_rnd(
        self.initial_buyable_hardware_performance,
        self.initial_buyable_hardware_performance,
        self.rnd_input_hardware[t_idx-1],
        self.cumulative_rnd_input_hardware[t_idx-1],
        self.hardware_returns,
        self.hardware_performance_ceiling
        )

      initial_hardware_improvement_rate = \
        improved_hardware_performance \
        / self.initial_buyable_hardware_performance

      self.initial_hardware_performance = \
        self.initial_buyable_hardware_performance \
        * initial_hardware_improvement_rate**self.hardware_delay_idx

      self.hardware_performance[0] = self.initial_hardware_performance

    self.hardware_performance[t_idx],\
    self.cumulative_rnd_input_hardware[t_idx] = \
      _update_rnd(
        self.hardware_performance[t_idx-1],
        self.initial_hardware_performance,
        self.rnd_input_hardware[t_idx-1],
        self.cumulative_rnd_input_hardware[t_idx-1],
        self.hardware_returns,
        self.hardware_performance_ceiling
        )

    # Software
    self.software[t_idx],\
    self.cumulative_rnd_input_software[t_idx] = \
      _update_rnd(
        self.software[t_idx-1],
        self.initial_software,
        self.rnd_input_software[t_idx-1],
        self.cumulative_rnd_input_software[t_idx-1],
        self.software_returns,
        self.software_ceiling
        )

  def allocate_fractional_inputs(self, t_idx):

    # Ramp-up detection (only if enabled)
    if self.rampup_enabled:
      self.rampup[t_idx] = \
        self.frac_tasks_automated_goods[t_idx-1] >= self.rampup_trigger
    else:
      self.rampup[t_idx] = False

    t_year = self.index_to_time(t_idx) - self.t_step
    if self.rampup[t_idx] and not self.rampup[t_idx-1]:
      self.rampup_start = t_year

    if self.frac_tasks_automated_goods[t_idx-1] >= 0.2 and \
    not self.frac_tasks_automated_goods[t_idx-2] >= 0.2:
      self.rampup_mid = t_year

    if self.frac_automatable_tasks_goods_no_tradeoff[t_idx-1] >= 0.2 and \
    not self.frac_automatable_tasks_goods_no_tradeoff[t_idx-2] >= 0.2:
      self.sub_agi_year = t_year

    if self.frac_automatable_tasks_goods_no_tradeoff[t_idx-1] >= 1 and \
    not self.frac_automatable_tasks_goods_no_tradeoff[t_idx-2] >= 1:
      self.agi_year = t_year

    # ------------------------------------------------------------
    # Cool-down detection: check relative increase in automation
    # over a window *T* years. If the increase is below a threshold
    # *x*, mark the current timestep as being in cool-down.
    # ------------------------------------------------------------

    cooldown_window = getattr(self, 'cooldown_window', 1.0)      # years
    cooldown_threshold = getattr(self, 'cooldown_threshold', 0.05)  # 5 % relative

    prev_cool = self.cooldown[t_idx-1] if t_idx > 1 else False
    self.cooldown[t_idx] = False
    window_idx = int(round(cooldown_window / self.t_step))
    if self.cooldown_enabled and t_idx - 1 - window_idx >= 0:
      auto_now = self.frac_tasks_automated_goods[t_idx-1]

      # Do not enter cool-down once the economy is fully automated
      if auto_now < 1.0:
        auto_prev = self.frac_tasks_automated_goods[t_idx-1-window_idx]
        if auto_prev > 0:
          rel_inc = (auto_now - auto_prev) / auto_prev
        else:
          rel_inc = np.inf  # Treat as large increase if base is ~0

        self.cooldown[t_idx] = rel_inc < cooldown_threshold

        # Store the first calendar year that cooldown begins
        if self.cooldown[t_idx] and not prev_cool:
          self.cooldown_start = t_year
      else:
        # Fully automated – never cool down afterwards
        self.cooldown[t_idx] = False

    # When cooldown happens, override rampup to False
    # (but preserve rampup_start for plotting purposes)
    if self.cooldown[t_idx]:
      self.rampup[t_idx] = False

    def update_frac_input(current_frac, growth_param, growth_param_rampup, growth_param_cooldown, max_frac):
      """Update a fractional input under normal, ramp-up, or cool-down."""
      if self.rampup[t_idx]:
        selected = growth_param_rampup
      elif self.cooldown[t_idx]:
        selected = growth_param_cooldown
      else:
        selected = growth_param

      rate = self._param_value(selected, t_idx, t_year)
      frac = current_frac * np.exp(self.t_step * rate)
      return min(frac, max_frac)

    # Hacky loop
    frac_metrics = [
      'frac_gwp_compute',
      'frac_capital_hardware_rnd',
      'frac_labour_hardware_rnd',
      'frac_compute_hardware_rnd',
      'frac_labour_software_rnd',
      'frac_compute_software_rnd',
      'frac_compute_training'
      ]

    for frac_metric in frac_metrics:
      getattr(self, frac_metric)[t_idx] = \
        update_frac_input(
          getattr(self, frac_metric)[t_idx-1],
          getattr(self, f'{frac_metric}_growth'),
          getattr(self, f'{frac_metric}_growth_rampup'),
          getattr(self, f'{frac_metric}_growth_cooldown', 0),
          getattr(self, f'{frac_metric}_ceiling'),
        )

    # Cap the growth of the fraction of FLOP before rampup
    if self.money_spent_training[t_idx-1] > self.money_cap_training_before_wakeup \
    and not self.rampup[t_idx-1]:
      self.frac_compute_training[t_idx] = self.frac_compute_training[t_idx-1]

    # Goods production fractional inputs
    self.frac_capital_goods[t_idx] = \
      1 - self.frac_capital_hardware_rnd[t_idx]
    self.frac_labour_goods[t_idx] = \
      1 - self.frac_labour_hardware_rnd[t_idx] - self.frac_labour_software_rnd[t_idx]
    self.frac_compute_goods[t_idx] = \
      1 - self.frac_compute_hardware_rnd[t_idx] - self.frac_compute_software_rnd[t_idx] \
        - self.frac_compute_training[t_idx]
 
  def calculate_total_inputs(self, t_idx):

    # Compute
    self.compute_investment[t_idx] = \
      self.gwp[t_idx-1] * self.frac_gwp_compute[t_idx] * self.t_step

    if t_idx-self.hardware_delay_idx >= 0:
      buyable_hardware_performance = \
        self.hardware_performance[t_idx-self.hardware_delay_idx]
    else:
      buyable_hardware_performance = \
        self.hardware_performance[0] * (self.hardware_performance[1] \
        / self.hardware_performance[0])**(t_idx-self.hardware_delay_idx)

    new_hardware = self.compute_investment[t_idx] * buyable_hardware_performance

    self.hardware[t_idx] = \
      self.hardware[t_idx-1] \
      * ((1.-self.compute_depreciation)**self.t_step) \
      + new_hardware
    self.compute[t_idx] = self.hardware[t_idx] * self.software[t_idx]

    # Non-compute inputs
    capital_investment = \
      self.gwp[t_idx-1] * self.investment_rate * self.t_step
    self.capital[t_idx] =\
      self.capital[t_idx-1] + capital_investment

    self.labour[t_idx] =\
      self.labour[t_idx-1] * np.exp(self.labour_growth * self.t_step)

    # Total factor production
    self.tfp_goods[t_idx] = \
      self.tfp_goods[t_idx-1] * np.exp(self.tfp_growth * self.t_step)
    self.tfp_rnd[t_idx] = \
      self.tfp_rnd[t_idx-1] * np.exp(self.tfp_growth * self.t_step)

    # Track money spent training
    self.money_spent_training[t_idx] = \
      self.compute[t_idx] * self.frac_compute_training[t_idx] / (self.software[t_idx] * buyable_hardware_performance)

  ###########################################################################

  ## AUXILIARY FUNCTIONS

  @staticmethod
  def quantiles_from_gap(top, gap):
      unit = gap**(1/7)
      quantiles = {
          1.0: top,
          0.5: top/(unit**4),
          0.2: top/(unit**7),
          0.1: top/(unit**8.5),
          0.05: top/(unit**9.5),
          0.0: top/(unit**10.5),
          }
      return quantiles

  @staticmethod
  def process_quantiles(quantile_dict, n_items):
    """ Input is a dictionary of quantiles {q1:v1, ..., qn:vn}
        Returns a numpy array of size n_items whose quantiles match the dictionary
        The rest of entries are geometrically interpolated
        Assumes vi > vj when qi > qj.
    """

    q = np.linspace(0, 1, n_items)

    keys = sorted(list(quantile_dict.keys()))
    values = sorted(list(quantile_dict.values()))

    # Logarithmic interpolation
    result = 10**np.interp(q, keys, np.log10(values))

    return result

  @staticmethod
  def add_steepness(full_requirements, gap, requirements, steepness):
    """ Takes an array of requirements and converts it into a sum of units
        steps separated by `steepness` OOMs, but maintaining both ends of the
        FLOP gap
    """

    if steepness == 0: return requirements

    gap_low = full_requirements/gap
    gap_high = full_requirements

    result = 10**(np.log10(gap_low) + np.ceil((np.log10(requirements) - np.log10(gap_low))/steepness) * steepness)
    result[result > gap_high] = gap_high

    return result

  @staticmethod
  def solve_allocation(L, C, β, ρ, η, AT):
      """
      Solve the input allocation problem for
      L = Labour budget
      C = Compute budget
      β = task weights
      ρ = labour / capital substitution parameter
      η = compute / labour substitution ratio
      AT = number of automatable tasks

      See description of solution at the end of the notebook
      We assume that
        * η is monotonically decreasing on its index.
        * task 0 is automatable
      """

      # Check assumptions
      assert np.all(np.diff(η) <= 0.)
      assert AT > 0

      # Preprocessing parameters
      N = len(β)
      σ = 1. / (1.-ρ)

      # Precompute partial sums

      # np.sum(β[I:]**σ)
      sums_β = np.zeros(N + 1)
      sums_β[:-1] = np.cumsum(β[::-1]**σ)[::-1]

      # np.sum(β[:I]**σ * η[:I]**(σ-1))
      sums_β_η = np.zeros(N + 1)
      sums_β_η[1:] = np.cumsum(β[:]**σ * η[:]**(σ-1))

      # Iterate over critical indices
      for I in range(AT):
        # Initialize
        labour_input_task = np.zeros(N)
        compute_input_task = np.zeros(N)

        ## Equation 20
        A = η[I]**σ * sums_β[I]
        B = sums_β_η[I]
        compute_input_task[I] =\
          (C*A - L*B) / (A + η[I]*B)

        ## Equation 18
        labour_input_task[I] =\
          (L + η[I]*compute_input_task[I]) \
          * (β[I]**σ / sums_β[I]) \
          - η[I]*compute_input_task[I]

        if labour_input_task[I] >= 0:
          ## Equation 17
          labour_input_task[I+1:] =\
            (L + η[I]*compute_input_task[I]) \
            * (β[I+1:]**σ / sums_β[I])

          ## Equation 14
          Z = sums_β_η[I+1]
          compute_input_task[:I] =\
            (C + labour_input_task[I]/η[I]) \
            * β[:I]**σ * η[:I]**(σ-1) / Z

          if I > 0 and compute_input_task[I] < 0:
              compute_input_task[I:] = 0
              compute_input_task[:I] =\
            C \
            * β[:I]**σ * η[:I]**(σ-1) / sums_β_η[I]

              labour_input_task[:I] = 0
              labour_input_task[I:] =\
            L \
            * (β[I:]**σ / sums_β[I])

          break
      else:
        # The critical index is the last one
        I = AT-1

        # Initialize
        labour_input_task = np.zeros(N)
        compute_input_task = np.zeros(N)

        ## Equations 14 & 15
        Z = sums_β_η[I+1]
        compute_input_task[:I+1] =\
          C * β[:I+1]**σ * η[:I+1]**(σ-1) / Z

        ## We assume LI = 0
        labour_input_task[I] = 0

        ## Equation 22
        labour_input_task[I+1:] =\
          L * (β[I+1:]**σ / sums_β[I+1])

      # Fix rounding error
      if np.all(labour_input_task==0):
        labour_input_task[-1] = L

      return labour_input_task, compute_input_task

  @staticmethod
  def odds_to_probs(o):
    """ Stable implementation of conversion between odds and probs
    """
    # For small outputs the odds are approx equal to the probs
    if o < 1e-10:
      p = o
      p_not = 1.-p
    # For big outputs the odds can be approx like this
    elif o > 1e10:
      p = 1 - 1/o
      p_not = 1/o
    else:
      p = 1/(1+1/o)
      p_not = 1.-p

    assert 0. <= p <= 1., f"o = {o}, p = {p}"
    assert 0. <= p_not <= 1., f"o = {o}, p_not = {p_not}"
    if o > 0:
      assert np.abs(p/p_not / o - 1.) < 1e-4
    return p, p_not

  @staticmethod
  def adjust_task_weights(
      capital,
      labour_task_input,
      compute_task_input,
      task_compute_to_labour_ratio,
      capital_substitution,
      labour_substitution,
      capital_to_cognitive_share_ratio,
      compute_to_labour_share_ratio,
    ):
    """ Computes the task weights that would result in a
        target capital_to_labour_share_ratio and compute_to_labour_share_ratio of the economy
    """

    # Compute inner task weights

    task_input = \
      labour_task_input + \
      task_compute_to_labour_ratio*compute_task_input

    labour_share = np.sum(
      labour_task_input * \
      task_input**(labour_substitution-1)
    )

    compute_share = np.sum(
      task_compute_to_labour_ratio * \
      compute_task_input * \
      task_input**(labour_substitution-1)
    )

    assert np.all(compute_task_input[1:] == 0.)
    assert labour_task_input[0] == 0.
    compute_to_labour_task_weight_ratio = \
      compute_to_labour_share_ratio * labour_share / compute_share

    compute_task_weight, labour_task_weight = \
      SimulateTakeOff.odds_to_probs(compute_to_labour_task_weight_ratio)

    n_labour_tasks = len(labour_task_input)-1
    inner_task_weights = \
      np.array([compute_task_weight] +
               [labour_task_weight for i in range(n_labour_tasks)]
               )

    # Compute outer task weights
    cognitive_input = \
      SimulateTakeOff.ces_production_function(
          task_input,
          inner_task_weights,
          labour_substitution
        )

    capital_share = capital**capital_substitution
    cognitive_share = cognitive_input**capital_substitution

    capital_to_cognitive_task_weight_ratio = \
      capital_to_cognitive_share_ratio * cognitive_share / capital_share

    capital_task_weight, cognitive_task_weight = \
      SimulateTakeOff.odds_to_probs(capital_to_cognitive_task_weight_ratio)
    outer_task_weights = np.array([capital_task_weight, cognitive_task_weight])

    return outer_task_weights, inner_task_weights

  @staticmethod
  def compute_shares(
          capital,
          labour_task_input,
          compute_task_input,
          capital_task_weights,
          labour_task_weights,
          task_compute_to_labour_ratio,
          capital_substitution,
          labour_substitution,
      ):

    # Compute inputs
    task_input = \
      labour_task_input + \
      compute_task_input*task_compute_to_labour_ratio

    cognitive_input = \
      SimulateTakeOff.ces_production_function(
          task_input,
          labour_task_weights,
          labour_substitution
        )

    # Compute capital and cognitive shares
    capital_task_weight = capital_task_weights[0]
    cognitive_task_weight = capital_task_weights[1]

    capital_share = capital_task_weight*capital**capital_substitution
    cognitive_share = cognitive_task_weight*cognitive_input**capital_substitution

    sum = capital_share + cognitive_share
    capital_share /= sum
    cognitive_share /=sum

    # Compute labour and compute shares
    labour_share = np.sum(labour_task_weights \
                   * labour_task_input \
                   * task_input**(labour_substitution-1))

    compute_share = np.sum(labour_task_weights \
                    * compute_task_input*task_compute_to_labour_ratio \
                    * task_input**(labour_substitution-1))

    sum = labour_share + compute_share
    labour_share /= sum
    compute_share /= sum

    labour_share *= cognitive_share
    compute_share *= cognitive_share

    return capital_share, cognitive_share, labour_share, compute_share

  def ces_production_function(inputs, alphas, rho, tfp=1):
    return tfp*np.sum(alphas*(inputs**rho) / alphas.sum())**(1./rho)

  def nested_ces_production_function(
    capital, cognitive_inputs,
    outer_weights, inner_weights,
    outer_rho, inner_rho,
    tfp=1):

    cognitive_output = SimulateTakeOff.ces_production_function(
      cognitive_inputs,
      inner_weights,
      inner_rho
    )

    production = tfp*SimulateTakeOff.ces_production_function(
      np.array([capital, cognitive_output]),
      outer_weights,
      outer_rho
    )

    return production

  @staticmethod
  def first_index(condition):
    if np.any(condition):
      return np.argmax(condition)
    return None # TODO return np.nan

  def first_time(self, condition):
    idx = SimulateTakeOff.first_index(condition)
    if idx is None: return np.nan
    return self.index_to_time(idx)

  def time_to_index(self, t):
    return round((t - self.t_start) / self.t_step)

  def index_to_time(self, idx):
    return self.t_start + idx * self.t_step

  ###########################################################################

  def compute_metrics(self):
    self.compute_takeoff_metrics()
    self.compute_timeline_metrics()

  timeline_metrics = [
    'sub_agi_year',
    'agi_year',
    'automation_gns_20%',
    'automation_gns_100%',
    'automation_rnd_20%',
    'automation_rnd_100%',
    'rampup_start',
  ]

  def compute_timeline_metrics(self):
    unsorted_metrics = {}

    for th in [0.2, 1.0]:
      t_year_gns = self.first_time(self.frac_tasks_automated_goods >= th)
      unsorted_metrics[f'automation_gns_{int(th*100)}%'] = t_year_gns

      t_year_rnd = self.first_time(self.frac_tasks_automated_rnd >= th)
      unsorted_metrics[f'automation_rnd_{int(th*100)}%'] = t_year_rnd

    unsorted_metrics['sub_agi_year'] = self.sub_agi_year
    unsorted_metrics['agi_year']     = self.agi_year
    unsorted_metrics['rampup_start'] = self.rampup_start

    print(f"Ramp-up start time: {self.rampup_start}")

    self.timeline_metrics = {}
    for k in SimulateTakeOff.timeline_metrics:
      v = unsorted_metrics[k]
      self.timeline_metrics[k] = v if (v is not None) else np.nan

  def length_between_thresholds(
        self,
        series1,
        series2,
    ):
    """ Utility function to measure the amount of time between
        two thresholds being crossed
    """
    if not np.any(series1) or not np.any(series2):
      return np.nan
    idx1 = np.argmax(series1)
    idx2 = np.argmax(series2)
    return (idx2 - idx1) * self.t_step

  takeoff_metrics = [
    'full_automation_gns',
    'full_automation_rnd',
    'sub_agi_to_agi',
    'cog_output_multiplier',
    'gwp_growth',
  ]

  def compute_takeoff_metrics(self):
    """ Computes indicator metrics measuring length of AI takeoff
    """
    # Initialize takeoff metrics dict
    self.takeoff_metrics = {}

    # Time from AI that can perform 20% of tasks to AI that can perform 100%.

    self.takeoff_metrics["full_automation_gns"] = \
      self.length_between_thresholds(
          self.frac_tasks_automated_goods > 0.2,
          self.frac_tasks_automated_goods >= 1.,
      )

    self.takeoff_metrics["full_automation_rnd"] = \
      self.length_between_thresholds(
          self.frac_tasks_automated_rnd > 0.2,
          self.frac_tasks_automated_rnd >= 1.,
      )

    # Time from powerful sub-AGI to AGI
    self.takeoff_metrics['sub_agi_to_agi'] = self.agi_year - self.sub_agi_year if (self.agi_year is not None) else np.nan

    # Years from "total cognitive output is 2X human cognitive output" to
    # "total cognitive output is 10X human cognitive output"
    self.takeoff_metrics["cog_output_multiplier"] = \
      self.length_between_thresholds(
          self.automation_multiplier_rnd > 2,
          self.automation_multiplier_rnd > 10,
      )

    # Time from 5% GWP growth to 20% GWP growth
    delta = int(1 / self.t_step)
    self.gwp_growth = np.log(self.gwp[delta:self.t_idx] / self.gwp[:self.t_idx-delta])
    self.takeoff_metrics["gwp_growth"] = \
      self.length_between_thresholds(
          self.gwp_growth > 0.05,
          self.gwp_growth > 0.20,
      )

    # GWP doubling times
    self.compute_doubling_times()

  def compute_doubling_times(self):
    self.doubling_times = [self.t_step / np.log2(self.gwp[1]/self.gwp[0])]

    if self.rampup_start is not None:
      reference_idx = self.time_to_index(self.rampup_start)
      for idx in range(reference_idx, len(self.timesteps)):
        if self.gwp[idx] > 2*self.gwp[reference_idx]:
          self.doubling_times.append(self.index_to_time(idx) - self.index_to_time(reference_idx))
          reference_idx = idx

    # Round doubling times
    self.doubling_times = [round(dt, 2) for dt in self.doubling_times]

    # We are only interested in the first five doubling times
    self.doubling_times = self.doubling_times[:5]

  ###########################################################################

  ## VISUALIZATION ##

  def plot(self, metric, plot_growth = False, new_figure=True, line_color='black', crop_after_full_automation = True, year_shift=0):
    """ Plot a metric over time.
        Eg gwp, compute, capital, labour, hardware_efficiency, software, hardware
    """
    x = self.timesteps

    if crop_after_full_automation:
      full_automation_year = self.timeline_metrics['automation_gns_100%']

      idx_end = min(self.time_to_index(full_automation_year+5), self.t_idx) \
                if not np.isnan(full_automation_year) and crop_after_full_automation else self.t_idx
      x = x[:idx_end]
      y = getattr(self, metric)[:idx_end]

    if plot_growth:
      # Plot annual growth
      delta = int(1 / self.t_step)
      x = x[delta:]
      y = np.log(y[delta:] / y[:-delta])

    if new_figure:
      plt.figure(figsize=(14, 8), dpi=80)
    #if not plot_growth:
    plt.yscale('log')
    plt.plot(x, y, color = line_color)
    if plot_growth:
      plt.title(f"{metric} growth over time")
    else:
      plt.title(f"{metric} over time")

    self._plot_vlines(line_color = line_color)

    # Shift x-axis labels without moving data
    if year_shift != 0:
      ax = plt.gca()
      ax.xaxis.set_major_formatter(_mticker.FuncFormatter(lambda val, pos: f"{int(val + year_shift)}"))
      ax.xaxis.set_major_locator(_mticker.MultipleLocator(1))

  def _plot_vlines(self, line_color = 'black'):
    rampup_start = self.timeline_metrics.get('rampup_start')
    if rampup_start is not None and not np.isnan(rampup_start):
      plt.axvline(rampup_start,
                linestyle='dotted',
                color='red',
                label='Wake up')

    rampup_mid = getattr(self, 'rampup_mid', None)
    if rampup_mid is not None:
      plt.axvline(self.rampup_mid,
                linestyle='-.',
                color=line_color,
                label='20% automation')

    if not np.isnan(self.timeline_metrics['automation_gns_100%']):
      plt.axvline(self.timeline_metrics['automation_gns_100%'],
                linestyle='dashed',
                color=line_color,
                label='100% automation')

    # Cool-down start vertical line
    if getattr(self, 'cooldown_start', None) is not None:
      plt.axvline(self.cooldown_start,
                linestyle='solid',
                color=line_color,
                label='Cool-down')

  def plot_compute_decomposition(self, new_figure = True, crop_after_full_automation = True, crop_at_year = None):
    """ Show the growth of the factors that drive compute
    """

    if new_figure:
      plt.figure(figsize=(14, 8), dpi=80)

    full_automation_year = self.timeline_metrics['automation_gns_100%']

    start_idx = 0
    reference_idx = self.time_to_index(self.rampup_start) if self.rampup_start is not None else 0
    if crop_at_year is not None:
      end_idx = self.time_to_index(crop_at_year)
    else:
      end_idx = min(self.time_to_index(full_automation_year+5), self.t_idx) if not np.isnan(full_automation_year) and crop_after_full_automation else self.t_idx

    plt.plot(self.timesteps[start_idx:end_idx], self.compute_investment[start_idx:end_idx]/self.compute_investment[reference_idx], label='$ on FLOP globally', color = 'blue')
    plt.plot(self.timesteps[start_idx:end_idx], self.hardware_performance[start_idx:end_idx]/self.hardware_performance[reference_idx], label='Hardware (FLOP/$)', color = 'orange')
    plt.plot(self.timesteps[start_idx:end_idx], self.software[start_idx:end_idx]/self.software[reference_idx], label='Software (2020-FLOP per FLOP)', color = 'green')
    plt.plot(self.timesteps[start_idx:end_idx], self.frac_compute_training[start_idx:end_idx]/self.frac_compute_training[reference_idx], label='Fraction global FLOP on training', color = 'red')

    plt.yscale('log')

    self._plot_vlines()

    # Plot horizontal lines showing each order of magnitude
    high = max(
            self.frac_compute_training[end_idx-1]/self.frac_compute_training[reference_idx],
            self.software[end_idx-1]/self.software[reference_idx],
            self.hardware_performance[end_idx-1]/self.hardware_performance[reference_idx],
            self.compute_investment[end_idx-1]/self.compute_investment[reference_idx]
            )
    low = min(
            self.frac_compute_training[start_idx]/self.frac_compute_training[reference_idx],
            self.software[start_idx]/self.software[reference_idx],
            self.hardware_performance[start_idx]/self.hardware_performance[reference_idx],
            self.compute_investment[start_idx]/self.compute_investment[reference_idx]
            )
    for oom in range(math.floor(np.log10(low)), math.ceil(np.log10(high))):
      plt.axhline(10**oom, linestyle='dotted', color='black',)

    if new_figure:
      plt.title(f"Compute increase decomposition")
      plt.legend(bbox_to_anchor=(1.02, 1), loc='upper left', borderaxespad=0)
      plt.tight_layout()

    # Super hacky
    return {
      'start_idx': start_idx,
      'reference_idx': reference_idx,
      'end_idx': end_idx,
    }

  def plot_compute_decomposition_bioanchors_style(self, new_figure = True):
    """ Show the growth of the factors that drive compute in the style of the Bio Anchors report
    """

    if new_figure:
      plt.figure(figsize=(14, 8), dpi=80)

    reference_idx = 0

    training_investment = self.compute_investment * self.frac_compute_training

    plt.plot(self.timesteps, training_investment/training_investment[reference_idx], label = 'Training compute investment', color = 'purple')
    plt.plot(self.timesteps, self.hardware_performance/self.hardware_performance[reference_idx], label = 'Hardware performance', color = 'orange')
    plt.plot(self.timesteps, self.software/self.software[reference_idx], label = 'Software', color = 'green')

    plt.yscale('log')

    utils.draw_oom_lines()

    if new_figure:
      plt.title(f"Compute increase decomposition")
      plt.legend(bbox_to_anchor=(1.02, 1), loc='upper left', borderaxespad=0)
      plt.tight_layout()

  def get_summary_table(self):
    summary_table = []

    prerampup = np.mean([self.t_start, self.rampup_start]) if self.rampup_start is not None else None
    raw_metrics = ['biggest_training_run', 'frac_tasks_automated_goods', 'frac_tasks_automated_rnd']
    doubling_time_metrics = ['hardware_performance', 'software', 'compute_investment', 'frac_compute_training', 'gwp', 'capital', 'labour', 'tfp_rnd', "rnd_input_software", "cumulative_rnd_input_software"]

    for period, t in {'prerampup': prerampup,
                      'rampup start': self.rampup_start,
                      'mid rampup': self.rampup_mid,
                      'full economic automation': self.timeline_metrics['automation_gns_100%']}.items() :

      if t is None or np.isnan(t):
        summary_row = {
          'period' : period,
          'year' : np.nan,
        }

        for raw_metric in raw_metrics:
          summary_row[f"{raw_metric}"] = np.nan

        for doubling_time_metric in doubling_time_metrics:
          summary_row[f"{doubling_time_metric} growth rate"] = np.nan
          summary_row[f"{doubling_time_metric} doubling time"] = np.nan

        summary_table.append(summary_row)
        continue

      idx = self.time_to_index(t)
      t = self.index_to_time(idx)
      t_end = t + 1
      idx_end = self.time_to_index(t_end)

      # If the [idx, idx_end] interval falls outside our simulation, move it to the left
      if idx_end >= len(self.timesteps):
        diff = idx_end - (len(self.timesteps) - 1)
        idx -= diff
        idx_end -= diff

      summary_row = {
        'period' : period,
        'year' : t,
      }

      # Auxiliary functions
      dt = lambda s : 1 / np.log2(s[idx_end]/s[idx]) \
                      if np.log2(s[idx_end]/s[idx]) != 0 else np.nan

      gr = lambda s : np.log(s[idx_end] / s[idx])

      for raw_metric in raw_metrics:
        summary_row[f"{raw_metric}"] = getattr(self, raw_metric)[idx]

      for doubling_time_metric in doubling_time_metrics:
        summary_row[f"{doubling_time_metric} growth rate"] = gr(getattr(self, doubling_time_metric))
        summary_row[f"{doubling_time_metric} doubling time"] = dt(getattr(self, doubling_time_metric))

      summary_table.append(summary_row)

    summary_table = pd.DataFrame(summary_table)

    return summary_table

  def display_summary_table(self):
    print(self.get_summary_table())

  def get_takeoff_metrics(self):
    metrics_df = pd.DataFrame(self.takeoff_metrics, index = [0])
    metrics_df["doubling times"] = repr(self.doubling_times[:4])
    return metrics_df

  def display_takeoff_metrics(self):
    print(self.get_takeoff_metrics())

  def plot_fractional_inputs(self):
    fracs = {
      'capital_fracs': {
        'frac_capital_hardware_rnd': self.frac_capital_hardware_rnd,
      },

      'compute_fracs': {
        'frac_compute_hardware_rnd': self.frac_compute_hardware_rnd,
        'frac_compute_software_rnd': self.frac_compute_software_rnd,
        'frac_compute_training': self.frac_compute_training,
      },

      'gwp_fracs': {
        'frac_gwp_compute': self.frac_gwp_compute,
      },

      'labour_fracs': {
        'frac_labour_hardware_rnd': self.frac_labour_hardware_rnd,
        'frac_labour_software_rnd': self.frac_labour_software_rnd,
      },
    }

    agi_idx = self.time_to_index(self.timeline_metrics['automation_gns_100%'] + 5)

    colors = plt.rcParams["axes.prop_cycle"].by_key()["color"]
    color_index = 0

    for type_index, frac_list in enumerate(fracs.values()):
      plt.subplot(len(fracs.values()), 1, type_index + 1)
      for frac_index, (name, frac) in enumerate(frac_list.items()):
        plt.plot(self.timesteps.data[:agi_idx], frac.data[:agi_idx], label = name, color = colors[color_index])
        color_index += 1
      plt.legend()

    # Set the title for the whole plot
    plt.suptitle('Fractional inputs')

  def _param_value(self, param, t_idx, t_year):
    """Return the value of a (potentially) time-varying parameter.

    Supported formats for *param*:
      • scalar (int/float) – interpreted as a constant value.
      • callable – called with *t_year* and its return value is used.
      • sequence (list/tuple/np.ndarray) – interpreted as one value per
        simulation step starting from *t_start*.  If the simulation runs
        longer than the provided sequence, the last element is repeated.
      • dict – keys are calendar years and the value for the latest year
        not greater than *t_year* is used (piece-wise constant).
    """
    # Constant scalar
    if np.isscalar(param):
      return param

    # Callable – let the user compute the value on the fly
    if callable(param):
      return param(t_year)

    # Sequence indexed by timestep
    if isinstance(param, (list, tuple, np.ndarray)):
      if t_idx < len(param):
        return param[int(t_idx)]
      # Past the end – keep the last value constant
      return param[-1]

    # Dict keyed by (calendar) year
    if isinstance(param, dict):
      if not param:
        raise ValueError("Time-series parameter dictionary is empty")
      years = sorted(param.keys())
      for y in reversed(years):
        if t_year >= y:
          return param[y]
      # If t_year precedes the earliest key, return the first value
      return param[years[0]]

    raise ValueError(f"Unsupported type for time-series parameter: {type(param)}")

if __name__ == "__main__":
  # Handle CLI arguments
  parser = init_cli_arguments()
  args = handle_cli_arguments(parser)

  # Determine parameters source (JSON file vs spreadsheet)
  if getattr(args, 'param_json', None):
    title, json_params = utils.load_parameters_from_json(args.param_json)
    model = SimulateTakeOff(**json_params, title=title)
  else:
    # Retrieve parameter estimates from spreadsheet
    parameter_table = get_parameter_table(tradeoff_enabled="from_spreadsheet")
    best_guess_parameters = {parameter : row['Best guess'] for parameter, row in parameter_table.iterrows()}
    model = SimulateTakeOff(**best_guess_parameters)
    # Ensure `title` is always defined so that plot customisations work
    title = getattr(model, 'title', None)

  model.run_simulation()

  # ----------------------------
  # Plot customisation settings
  # ----------------------------
  # If the JSON title corresponds to the "updated_conservative" scenario we want
  # dedicated plot styling: only show every 5 calendar years on the x-axis and
  # truncate all figures at (calendar) year 2050.
  updated_conservative = (title == 'updated_conservative')
  _YEAR_SHIFT = 3  # All existing plots display years shifted by +3
  _PLOT_CAP_YEAR = 2100
  _PLOT_CAP_X = _PLOT_CAP_YEAR - _YEAR_SHIFT  # coordinate after shift

  def _customise_axis(ax):
    """Apply axis customizations."""
    _PLOT_START_YEAR = 2025
    _PLOT_START_X = _PLOT_START_YEAR - _YEAR_SHIFT # This is 2022

    # Set the left boundary for all cases
    ax.set_xlim(left=_PLOT_START_X)

    if updated_conservative:
      # Apply additional customizations for the conservative case
      ax.xaxis.set_major_locator(_mticker.MultipleLocator(5))
      
      # Cap the right boundary
      _, xmax = ax.get_xlim()
      ax.set_xlim(right=min(xmax, _PLOT_CAP_X))

  # -------------------------------------------------------------------------
  # Plot things
  # -------------------------------------------------------------------------
  # Plot things
  model.plot('gwp', year_shift=3)
  # Label the main GWP curve for the legend
  if plt.gca().lines:
    plt.gca().lines[0].set_label('GWP')
  plt.legend()
  _customise_axis(plt.gca())

  # Save the figure (do not display it)
  import os
  # Choose output directory based on scenario title
  if updated_conservative:
    _PLOT_DIR_NAME = 'conservative_plots'
  elif title == 'updated_baseline':
    _PLOT_DIR_NAME = 'baseline_plots'
  else:
    _PLOT_DIR_NAME = 'plots'

  # Ensure plots are stored inside the ftm/ftm package directory (one level above this file)
  _BASE_PLOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
  plot_dir = os.path.join(_BASE_PLOT_DIR, _PLOT_DIR_NAME)
  os.makedirs(plot_dir, exist_ok=True)
  plt.savefig(os.path.join(plot_dir, 'gwp.png'), dpi=300, bbox_inches='tight')
  plt.close('all')

  # Automation percentage plot (Goods and R&D)
  plt.figure(figsize=(14,8), dpi=80)
  full_auto_year = model.timeline_metrics['automation_gns_100%']
  if np.isnan(full_auto_year):
    idx_end = model.t_idx
  else:
    idx_end = model.time_to_index(full_auto_year + 1)  # one year after full automation
    idx_end = min(idx_end, model.t_idx)

  x = model.timesteps[:idx_end]
  plt.plot(x, model.frac_tasks_automated_goods[:idx_end] * 100, label='Goods', color='blue')
  plt.plot(x, model.frac_tasks_automated_rnd[:idx_end] * 100, label='R&D', color='orange')
  plt.ylabel('Percentage of tasks automated')
  plt.ylim(0, 100)
  model._plot_vlines()
  # Shift x-axis labels
  import matplotlib.ticker as _mticker
  ax = plt.gca()
  ax.xaxis.set_major_formatter(_mticker.FuncFormatter(lambda val, pos: f"{int(val + 3)}"))
  ax.xaxis.set_major_locator(_mticker.MultipleLocator(1))
  # Y-axis: ticks every 5 percentage points
  ax.yaxis.set_major_locator(_mticker.MultipleLocator(5))
  plt.legend()
  plt.title('Automation percentage over time')
  _customise_axis(ax)

  plt.savefig(os.path.join(plot_dir, 'automation_percent.png'), dpi=300, bbox_inches='tight')
  plt.close('all')

  # No additional plots are generated or shown

  # Largest training run plot (FLOP per year)
  plt.figure(figsize=(14,8), dpi=80)
  # Cap the x-axis at the year 2035 (or end of simulation if sooner)
  cap_year = 2100 if updated_conservative else 2035
  idx_end_lr = min(model.time_to_index(cap_year), model.t_idx)
  x = model.timesteps[:idx_end_lr]
  plt.plot(x, model.biggest_training_run[:idx_end_lr], color='green', label='Largest training run')
  # Highlight peak and plateau if the series declines afterwards
  y_lr = model.biggest_training_run[:idx_end_lr]
  if len(y_lr) > 1:
    peak_idx = int(np.argmax(y_lr))
    peak_y = y_lr[peak_idx]
    # Check whether the series declines after the peak
    if peak_idx < len(y_lr) - 1 and y_lr[-1] < peak_y:
      peak_x = x[peak_idx]
      # Peak line and annotation removed as requested
  # Use logarithmic scale so the curve isn't squashed near zero and set a sensible lower bound
  plt.yscale('log')
  if len(model.biggest_training_run) > 0:
    ymin = model.biggest_training_run[0] * 0.8  # start slightly below first value
    ax = plt.gca()
    ymax = ax.get_ylim()[1]
    ax.set_ylim(bottom=ymin, top=ymax)
  ax = plt.gca()
  ax.xaxis.set_major_formatter(_mticker.FuncFormatter(lambda val, pos: f"{int(val + 3)}"))
  ax.xaxis.set_major_locator(_mticker.MultipleLocator(1))
  if len(x) > 0:
    ax.set_xlim(left=x[0])
  plt.title('Largest training run over time')
  # Add vertical milestone lines (wake-up, 20 % & 100 % automation)
  model._plot_vlines()
  plt.legend()
  _customise_axis(ax)
  plt.savefig(os.path.join(plot_dir, 'largest_training_run.png'), dpi=300, bbox_inches='tight')
  plt.close('all')

  # ----------------------------
  # GWP yearly growth rate plot
  # ----------------------------
  delta_idx = int(1 / model.t_step)
  cap_year_growth = 2100 if updated_conservative else 2035
  idx_end_growth = min(model.time_to_index(cap_year_growth), model.t_idx)

  x_growth = model.timesteps[delta_idx:idx_end_growth]
  y_growth = np.log(model.gwp[delta_idx:idx_end_growth] / model.gwp[:idx_end_growth-delta_idx])

  plt.figure(figsize=(14, 8), dpi=80)
  plt.plot(x_growth, y_growth, color='black', label='GWP growth')
  model._plot_vlines()

  ax = plt.gca()
  ax.xaxis.set_major_formatter(_mticker.FuncFormatter(lambda val, pos: f"{int(val + 3)}"))
  # Use 2-year ticks to avoid crowded labels
  ax.xaxis.set_major_locator(_mticker.MultipleLocator(2))
  plt.legend()
  plt.title('GWP yearly growth rate')
  plt.ylabel('ln growth per year')
  _customise_axis(ax)
  plt.savefig(os.path.join(plot_dir, 'gwp_growth.png'), dpi=300, bbox_inches='tight')
  plt.close('all')

  # ----------------------------------------
  # Largest training run yearly growth plot
  # ----------------------------------------
  idx_end_lr_growth = idx_end_lr  # same cap year selected above
  if idx_end_lr_growth - delta_idx > 0:
    x_lr_growth = model.timesteps[delta_idx:idx_end_lr_growth]
    y_lr_growth = np.log(model.biggest_training_run[delta_idx:idx_end_lr_growth] / model.biggest_training_run[:idx_end_lr_growth-delta_idx])

    plt.figure(figsize=(14, 8), dpi=80)
    plt.plot(x_lr_growth, y_lr_growth, color='green', label='Largest training run growth')
    model._plot_vlines()

    ax = plt.gca()
    ax.xaxis.set_major_formatter(_mticker.FuncFormatter(lambda val, pos: f"{int(val + 3)}"))
    ax.xaxis.set_major_locator(_mticker.MultipleLocator(1))
    plt.legend()
    plt.title('Largest training run yearly growth rate')
    plt.ylabel('ln growth per year')
    _customise_axis(ax)
    plt.savefig(os.path.join(plot_dir, 'largest_training_run_growth.png'), dpi=300, bbox_inches='tight')
    plt.close('all')
