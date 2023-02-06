import numpy as np
import openmdao.api as om
from scipy.integrate import solve_ivp

from ...options import options as dymos_options

from .ode_evaluation_group import ODEEvaluationGroup


class ODEIntegrationComp(om.ExplicitComponent):
    """
    A component to perform explicit integration with a generic ODE integrator/IVP solver.

    This component contains a sub-Problem with a component that will be solved over num_nodes
    points instead of creating num_nodes instances of that same component and connecting them
    together.

    Parameters
    ----------
    input_grid_data : GridData
        The GridData which defines the nodes at which the control inputs values are specified.
    time_options : OptionsDictionary
        OptionsDictionary of time options.
    state_options : dict of {str: OptionsDictionary}
        For each state variable, a dictionary of its options, keyed by name.
    parameter_options : dict of {str: OptionsDictionary}
        For each parameter, a dictionary of its options, keyed by name.
    control_options : dict of {str: OptionsDictionary}
        For each control variable, a dictionary of its options, keyed by name.
    polynomial_control_options : dict of {str: OptionsDictionary}
        For each polynomial variable, a dictionary of its options, keyed by name.
    output_grid_data : GridData
        The GridData which defines the nodes at which the outputs of the integration are provided, or None if
        the input_grid_data is to be used.
    reports : bool or None or str or Sequence
        Controls the reports generated by the subproblems used during the integration.
    standalone_mode : bool
        If True, assume this component is being run as its own system. As part of the Dymos
        ShootingPhase, this system needs to be setup during the Phase configure process and setting this
        to False will enable that behavior.
    **kwargs : dict
        Additional keyword arguments passed to Group.

    Notes
    -----
    This code includes the following unicode symbols:
    theta:  U+03B8
    """
    def __init__(self, input_grid_data, time_options, state_options, parameter_options=None, control_options=None,
                 polynomial_control_options=None, output_grid_data=None, reports=False, standalone_mode=True, **kwargs):
        super().__init__(**kwargs)
        self.time_options = time_options
        self.state_options = state_options
        self.parameter_options = parameter_options or {}
        self.control_options = control_options or {}
        self.polynomial_control_options = polynomial_control_options or {}
        self._eval_subprob = None
        self._input_grid_data = input_grid_data
        self._output_grid_data = output_grid_data if output_grid_data is not None else input_grid_data
        self._reports = reports
        self._standalone_mode = standalone_mode

        self._inputs_cache = ''

        self.x_size = 0
        self.p_size = 0
        self.u_size = 0
        self.up_size = 0
        self.theta_size = 0
        self.z_size = 0

        self._state_rate_of_names = []
        self._totals_of_names = []
        self._totals_wrt_names = []

        self._no_check_partials = not dymos_options['include_check_partials']
        self._num_control_input_nodes = input_grid_data.subset_num_nodes['control_input']

    def initialize(self):
        """
        Declare options for the ODEIntegrationComp.
        """
        self.options.declare('ode_class', desc='System defining the ODE', recordable=False)
        self.options.declare('method', default='DOP853', desc='The integration method used.')
        self.options.declare('atol', types=float, default=1.0E-6)
        self.options.declare('rtol', types=float, default=1.0E-9)
        self.options.declare('first_step', types=float, allow_none=True, default=None)
        self.options.declare('max_step', types=float, default=np.inf)
        self.options.declare('propagate_derivs', types=bool, default=True,
                             desc='If True, propagate the state and derivatives of the state and time with respect to '
                                  'the integration parameters. If False, only propagate the primal states.')
        self.options.declare('ode_init_kwargs', types=dict, allow_none=True, default=None)

    def _setup_subprob(self):
        self._eval_subprob = p = om.Problem(comm=self.comm, reports=self._reports)
        p.model.add_subsystem('ode_eval',
                              ODEEvaluationGroup(ode_class=self.options['ode_class'],
                                                 time_options=self.time_options,
                                                 state_options=self.state_options,
                                                 parameter_options=self.parameter_options,
                                                 control_options=self.control_options,
                                                 polynomial_control_options=self.polynomial_control_options,
                                                 ode_init_kwargs=self.options['ode_init_kwargs'],
                                                 input_grid_data=self._input_grid_data),
                              promotes_inputs=['*'],
                              promotes_outputs=['*'])

        p.setup()
        p.final_setup()

    def _set_segment_index(self, idx):
        """
        Set the index of the segment being integrated.
        """
        self._eval_subprob.model._get_subsystem('ode_eval').set_segment_index(idx)

    def _setup_time(self):
        if self._standalone_mode:
            self._configure_time()

    def _configure_time(self):
        """
        Components do not have configure methods, but since we rely on configure-time introspection to determine
        properties of the states, times, controls, parameters, and timeseries, we need to call this method at
        configure time in the parent ExplicitShooting transcription object.
        """
        num_output_rows = self._num_output_rows
        t_units = self.time_options['units']
        t_name = self.time_options['name']

        self._totals_of_names.append(t_name)
        self._totals_wrt_names.extend([t_name, 't_initial', 't_duration'])

        self.add_input('t_initial', shape=(1,), units=t_units)
        self.add_input('t_duration', shape=(1,), units=t_units)
        self.add_output('t_final', shape=(1,), units=t_units)
        self.add_output(t_name, shape=(num_output_rows, 1), units=t_units)
        self.add_output(f'{t_name}_phase', shape=(num_output_rows, 1), units=t_units)

        self.declare_partials('t_final', ['t_initial', 't_duration'], val=1.0)
        self.declare_partials(t_name, ['t_initial', 't_duration'], val=1.0)
        self.declare_partials(f'{t_name}_phase', 't_duration', val=1.0)

    def _setup_states(self):
        if self._standalone_mode:
            self._configure_states()

    def _configure_states(self):
        """
        Components do not have configure methods, but since we rely on configure-time introspection to determine
        properties of the states, times, controls, parameters, and timeseries, we need to call this method at
        configure time in the parent ExplicitShooting transcription object.
        """
        num_output_rows = self._num_output_rows

        # The total size of the entire state vector
        self.x_size = 0

        self._state_input_names = {}
        self._state_output_names = {}

        # The indices of each state in x
        self.state_idxs = {}

        # The indices of each state's initial value in z
        self._state_idxs_in_z = {}

        for state_name, options in self.state_options.items():
            self._state_input_names[state_name] = f'states:{state_name}'
            self._state_output_names[state_name] = f'states_out:{state_name}'

            # Keep track of the derivative "of" names for state rates separately, so we don't
            # request them when they're not necessary.
            self._state_rate_of_names.append(f'state_rate_collector.state_rates:{state_name}_rate')
            self._totals_wrt_names.append(self._state_input_names[state_name])

            self.add_input(self._state_input_names[state_name],
                           shape=options['shape'],
                           units=options['units'],
                           desc=f'initial value of state {state_name}')
            self.add_output(self._state_output_names[state_name],
                            shape=(num_output_rows,) + options['shape'],
                            units=options['units'],
                            desc=f'final value of state {state_name}')

            state_size = np.prod(options['shape'], dtype=int)

            # The indices of the state in x
            self.state_idxs[state_name] = np.s_[self.x_size:self.x_size + state_size]
            self.x_size += state_size

            self.declare_partials(of=self._state_output_names[state_name],
                                  wrt='t_initial')

            self.declare_partials(of=self._state_output_names[state_name],
                                  wrt='t_duration')

            for state_name_wrt in self.state_options:
                self.declare_partials(of=self._state_output_names[state_name],
                                      wrt=f'states:{state_name_wrt}')

            for param_name_wrt in self.parameter_options:
                self.declare_partials(of=self._state_output_names[state_name],
                                      wrt=f'parameters:{param_name_wrt}')

            for control_name_wrt in self.control_options:
                self.declare_partials(of=self._state_output_names[state_name],
                                      wrt=f'controls:{control_name_wrt}')

            for control_name_wrt in self.polynomial_control_options:
                self.declare_partials(of=self._state_output_names[state_name],
                                      wrt=f'polynomial_controls:{control_name_wrt}')

    def _setup_parameters(self):
        if self._standalone_mode:
            self._configure_parameters()

    def _configure_parameters(self):
        """
        Components do not have configure methods, but since we rely on configure-time introspection to determine
        properties of the states, times, controls, parameters, and timeseries, we need to call this method at
        configure time in the parent ExplicitShooting transcription object.
        """
        # The indices of each parameter in p
        self.p_size = 0
        self.parameter_idxs = {}
        self._parameter_idxs_in_theta = {}
        self._parameter_idxs_in_z = {}
        self._param_input_names = {}

        for param_name, options in self.parameter_options.items():
            self._param_input_names[param_name] = f'parameters:{param_name}'
            self._totals_wrt_names.append(self._param_input_names[param_name])

            self.add_input(self._param_input_names[param_name],
                           shape=options['shape'],
                           val=options['val'],
                           units=options['units'],
                           desc=f'value for parameter {param_name}')

            param_size = np.prod(options['shape'], dtype=int)
            self.parameter_idxs[param_name] = np.s_[self.p_size:self.p_size+param_size]
            self.p_size += param_size

    def _setup_controls(self):
        if self._standalone_mode:
            self._configure_controls()

    def _configure_controls(self):
        """
        Components do not have configure methods, but since we rely on configure-time introspection to determine
        properties of the states, times, controls, parameters, and timeseries, we need to call this method at
        configure time in the parent ExplicitShooting transcription object.
        """
        self.u_size = 0
        self._control_idxs_in_theta = {}
        self._control_idxs_in_z = {}
        self._control_input_names = {}

        for control_name, options in self.control_options.items():
            control_param_shape = (self._num_control_input_nodes,) + options['shape']
            control_param_size = np.prod(control_param_shape, dtype=int)
            self._control_input_names[control_name] = f'controls:{control_name}'

            self._totals_wrt_names.append(self._control_input_names[control_name])

            self.add_input(self._control_input_names[control_name],
                           shape=control_param_shape,
                           units=options['units'],
                           desc=f'values for control {control_name} at input nodes')

            self.u_size += control_param_size

    def _setup_polynomial_controls(self):
        if self._standalone_mode:
            self._configure_polynomial_controls()

    def _configure_polynomial_controls(self):
        """
        Components do not have configure methods, but since we rely on configure-time introspection to determine
        properties of the states, times, controls, parameters, and timeseries, we need to call this method at
        configure time in the parent ExplicitShooting transcription object.
        """
        self.up_size = 0
        self._polynomial_control_idxs_in_theta = {}
        self._polynomial_control_idxs_in_z = {}
        self._polynomial_control_input_names = {}

        for name, options in self.polynomial_control_options.items():
            num_input_nodes = options['order'] + 1
            control_param_shape = (num_input_nodes,) + options['shape']
            control_param_size = np.prod(control_param_shape, dtype=int)

            self._polynomial_control_input_names[name] = f'polynomial_controls:{name}'

            self._totals_wrt_names.append(self._polynomial_control_input_names[name])

            self.add_input(self._polynomial_control_input_names[name],
                           shape=control_param_shape,
                           units=options['units'],
                           desc=f'values for control {name} at input nodes')

            self.up_size += control_param_size

    def _build_dx_dz_idxs(self):
        self._partial_dx_dz_idxs = {}

        dx_dz_idx = 0
        for output_state, output_state_options in self.state_options.items():
            output_name = self._state_output_names[output_state]
            output_state_size = np.prod(output_state_options['shape'])

            # Column indices wrt each initial state value
            for input_state, input_state_options in self.state_options.items():
                input_name = self._state_input_names[input_state]
                input_size = np.prod(input_state_options['shape'])
                idxs = np.s_[:, dx_dz_idx: dx_dz_idx + output_state_size * input_size]
                self._partial_dx_dz_idxs[output_name, input_name] = idxs
                dx_dz_idx += output_state_size * input_size

            # Column indices wrt t_initial and t_duration
            self._partial_dx_dz_idxs[output_name, 't_initial'] = np.s_[:, dx_dz_idx]
            self._partial_dx_dz_idxs[output_name, 't_duration'] = np.s_[:, dx_dz_idx + 1]
            dx_dz_idx += 2

            # Column indices wrt the parameters
            for param, param_options in self.parameter_options.items():
                input_name = self._param_input_names[param]
                input_size = np.prod(param_options['shape'])
                idxs = np.s_[:, dx_dz_idx: dx_dz_idx + output_state_size * input_size]
                self._partial_dx_dz_idxs[output_name, input_name] = idxs
                dx_dz_idx += output_state_size * input_size

            # Column indices wrt the controls
            for control, control_options in self.control_options.items():
                input_name = self._control_input_names[control]
                input_size = np.prod(control_options['shape']) * self._input_grid_data.subset_num_nodes['control_input']
                idxs = np.s_[:, dx_dz_idx: dx_dz_idx + output_state_size * input_size]
                self._partial_dx_dz_idxs[output_name, input_name] = idxs
                dx_dz_idx += output_state_size * input_size

            # Column indices wrt the polynomial controls
            for pc, pc_options in self.polynomial_control_options.items():
                input_name = self._polynomial_control_input_names[pc]
                input_size = np.prod(pc_options['shape']) * (pc_options['order'] + 1)
                idxs = np.s_[:, dx_dz_idx: dx_dz_idx + output_state_size * input_size]
                self._partial_dx_dz_idxs[output_name, input_name] = idxs
                dx_dz_idx += output_state_size * input_size

    def _setup_storage(self):
        if self._standalone_mode:
            self._configure_storage()

    def _configure_storage(self):
        igd = self._input_grid_data
        ogd = self._output_grid_data
        control_input_node_ptau = igd.node_ptau[igd.subset_node_indices['control_input']]

        # allocate the ODE parameter vector
        self.theta_size = 2 + self.p_size + self.u_size + self.up_size

        # allocate the integration parameter vector
        self.z_size = self.x_size + self.theta_size

        start_z = 0

        x_output_names = []
        z_input_names = []

        for state_name, options in self.state_options.items():
            state_size = np.prod(options['shape'], dtype=int)
            self._state_idxs_in_z[state_name] = np.s_[start_z: start_z + state_size]
            x_output_names.extend([self._state_output_names[state_name]] * state_size)
            z_input_names.extend([self._state_input_names[state_name]] * state_size)
            start_z += state_size

        # Add 2 to account for t_initial, t_duration
        start_z = self.x_size + 2
        start_theta = 2
        z_input_names.extend(['t_initial', 't_duration'])

        for param_name, options in self.parameter_options.items():
            param_size = np.prod(options['shape'], dtype=int)
            self._parameter_idxs_in_z[param_name] = np.s_[start_z: start_z + param_size]
            self._parameter_idxs_in_theta[param_name] = np.s_[start_theta: start_theta+param_size]
            z_input_names.extend([self._param_input_names[param_name]] * param_size)
            start_z += param_size
            start_theta += param_size

        for control_name, options in self.control_options.items():
            control_param_shape = (len(control_input_node_ptau),) + options['shape']
            control_param_size = np.prod(control_param_shape, dtype=int)
            self._control_idxs_in_z[control_name] = np.s_[start_z:start_z + control_param_size]
            self._control_idxs_in_theta[control_name] = np.s_[start_theta:start_theta+control_param_size]
            z_input_names.extend([self._control_input_names[control_name]] * control_param_size)
            start_z += control_param_size
            start_theta += control_param_size

        for pc_name, options in self.polynomial_control_options.items():
            num_input_nodes = options['order'] + 1
            control_param_shape = (num_input_nodes,) + options['shape']
            control_param_size = np.prod(control_param_shape, dtype=int)
            self._polynomial_control_idxs_in_z[pc_name] = np.s_[start_z:start_z + control_param_size]
            self._polynomial_control_idxs_in_theta[pc_name] = np.s_[start_theta:start_theta+control_param_size]
            z_input_names.extend([self._polynomial_control_input_names[pc_name]] * control_param_size)

            start_z += control_param_size
            start_theta += control_param_size

        # Allocate caches to store integrated quantities so we don't have to integrate the same inputs twice
        # if partials are requested for the same inputs as a compute.

        self._nnps = ogd.subset_num_nodes_per_segment['all']

        nn = sum(self._nnps)
        self._t_out = np.zeros((nn, 1))
        self._x_out = np.zeros((nn, self.x_size))

        if self.options['propagate_derivs']:
            self._dx_dz_out = np.zeros((nn, self.x_size * self.z_size))
            self._dt_dz_out = np.zeros((nn, self.z_size))
        else:
            self._dx_dz_out = None
            self._dt_dz_out = None

        # Build a map for obtaining the partials from dx_dz
        self._build_dx_dz_idxs()

        # Allocate the initial values for dx_dz and dt_dz
        self._dx_dz_0 = np.zeros((self.x_size, self.z_size))
        self._dx_dz_0[:, :self.x_size] = np.eye(self.x_size)

        self._dt_dz_0 = np.zeros((1, self.z_size))
        self._dt_dz_0[0, self.x_size] = 1.

        self._dtheta_dz = np.zeros((self.theta_size, self.z_size))
        self._dtheta_dz[:, -self.theta_size:] = np.eye(self.theta_size)

    def setup(self):
        """
        Add the necessary I/O and storage for the ODEIntegrationComp.
        """
        ogd = self._output_grid_data

        # The segment distribution needs to be the same in from the input grid to the output grid.
        if not self._input_grid_data.is_aligned_with(self._output_grid_data):
            raise RuntimeError(f'{self.pathname}: The input grid and the output grid must have the same number of '
                               f'segments and segment spacing, but the input grid segment ends are '
                               f'\n{self._input_grid_data.segment_ends}\n and the output grid segment ends are \n'
                               f'{self._output_grid_data.segment_ends}.')

        self._num_output_rows = ogd.subset_num_nodes['all']

        self._totals_of_names = []
        self._totals_wrt_names = []

        self._setup_subprob()
        self._setup_time()
        self._setup_parameters()
        self._setup_controls()
        self._setup_polynomial_controls()
        self._setup_states()
        self._setup_storage()

    def _subprob_run_model(self, x, t, theta, linearize=True):
        """
        Set inputs to the model given x, t, and theta, evaluate the model, and linearize if requested.

        Parameters
        ----------
        x : np.ndarray
            A flattened, contiguous vector of the state values.
        t : float
            The current time of the integration.
        theta : np.ndarray
            A flattened, contiguous vector of the ODE parameter values.
        linearize : bool
            If True, linearize the model after calling run_model.

        Returns
        -------

        """
        subprob = self._eval_subprob
        t_units = self.time_options['units']
        t_name = self.time_options['name']

        # transcribe time
        subprob.set_val(t_name, t, units=t_units)
        subprob.set_val('t_initial', theta[0], units=t_units)
        subprob.set_val('t_duration', theta[1], units=t_units)

        # transcribe states
        for name in self.state_options:
            input_name = self._state_input_names[name]
            subprob.set_val(input_name, x[0, self.state_idxs[name]])

        # transcribe parameters
        for name in self.parameter_options:
            input_name = self._param_input_names[name]
            subprob.set_val(input_name, theta[self._parameter_idxs_in_theta[name]])

        # transcribe controls
        for name in self.control_options:
            input_name = self._control_input_names[name]
            subprob.set_val(input_name, theta[self._control_idxs_in_theta[name]])

        for name in self.polynomial_control_options:
            input_name = self._polynomial_control_input_names[name]
            subprob.set_val(input_name, theta[self._polynomial_control_idxs_in_theta[name]])

        # Re-run in case the inputs have changed.
        subprob.run_model()

        if linearize:
            subprob.model._linearize(None)

    def eval_ode(self, x, t, theta, eval_solution=True, eval_derivs=True):
        """
        Evaluate the derivative of the ODE output rates wrt the inputs.

        Note that the control parameterization `u` undergoes an interpolation to provide the
        control values at any given time.  The ODE is then a function of these interpolated control
        values, we'll call them `u_hat`.  Technically, the derivatives wrt to `u` need to be chained
        together, but in this implementation the interpolation is part of the execution of the ODE
        and the chained derivatives are captured correctly there.

        Parameters
        ----------
        x : np.ndarray
            A flattened, contiguous vector of the state values.
        t : float
            The current time of the integration.
        theta : np.ndarray
            A flattened, contiguous vector of the ODE parameter values.
        eval_solution : bool
            If True, return the state rate output values, otherwise the first output will be None.
        eval_derivs : bool
            If True, return the derivatives of the state rate outputs wrt x, t, and theta. Otherwise the final
            three outputs will be None.

        Returns
        -------
        f : np.ndarray
            The outputs of the ODE state rates, or None if eval_solution is False.
        f_x : np.ndarray
            A matrix of the derivative of each element of the rates `f` wrt each value in `x`, or None
            if eval_derivs is False.
        f_t : np.ndarray
            A matrix of the derivatives of each element of the rates `f` wrt `time`, or None
            if eval_derivs is False.
        f_theta : np.ndarray
            A matrix of the derivatives of each element of the rates `f` wrt the parameters `theta`, or None
            if eval_derivs is False.
        """
        t_name = self.time_options['name']
        self._subprob_run_model(x, t, theta, linearize=False)

        # pack the resulting array
        if eval_solution:
            f = np.zeros((self.x_size, 1))
            for name in self.state_options:
                f[self.state_idxs[name]] = self._eval_subprob.get_val(
                    f'state_rate_collector.state_rates:{name}_rate').ravel()
        else:
            f = None

        if eval_derivs:
            f_t = np.zeros((self.x_size, 1))
            f_x = np.zeros((self.x_size, self.x_size))
            f_theta = np.zeros((self.x_size, self.theta_size))

            totals = self._eval_subprob.compute_totals(of=self._state_rate_of_names,
                                                       wrt=self._totals_wrt_names,
                                                       use_abs_names=False)

            for state_name in self.state_options:
                of_name = f'state_rate_collector.state_rates:{state_name}_rate'
                idxs = self.state_idxs[state_name]

                f_t[self.state_idxs[state_name]] = totals[of_name, t_name]

                for state_name_wrt in self.state_options:
                    idxs_wrt = self.state_idxs[state_name_wrt]
                    px_px = totals[of_name, self._state_input_names[state_name_wrt]]
                    f_x[idxs, idxs_wrt] = px_px.ravel()

                f_theta[idxs, 0] = totals[of_name, 't_initial']
                f_theta[idxs, 1] = totals[of_name, 't_duration']

                for param_name_wrt in self.parameter_options:
                    idxs_wrt = self._parameter_idxs_in_theta[param_name_wrt]
                    px_pp = totals[of_name, self._param_input_names[param_name_wrt]]
                    f_theta[idxs, idxs_wrt] = px_pp.ravel()

                for control_name_wrt in self.control_options:
                    idxs_wrt = self._control_idxs_in_theta[control_name_wrt]
                    px_pu = totals[of_name, self._control_input_names[control_name_wrt]]
                    f_theta[idxs, idxs_wrt] = px_pu.ravel()

                for pc_name_wrt in self.polynomial_control_options:
                    idxs_wrt = self._polynomial_control_idxs_in_theta[pc_name_wrt]
                    px_ppc = totals[of_name, self._polynomial_control_input_names[pc_name_wrt]]
                    f_theta[idxs, idxs_wrt] = px_ppc.ravel()

        else:
            f_x = f_t = f_theta = None

        return f, f_x, f_t, f_theta

    def _f_augmented(self, t, y, theta, dtheta_dz):
        """
        The ODE-callable function where y is the augmented state vector, theta are the ODE parameters, and dtheta_dz
        are the sensitivities of the ODE parameters to the integration parameters.

        Parameters
        ----------
        t : float
            The current value of the integration variable.
        y : np.array
            The augmented state vector.
        theta : np.array
            The ODE parameter vector. The first two elements are t_initial and t_duration.
        dtheta_dz : np.array
            The sensitivities of the ODE parameters wrt the integration parameters. Since the ODE parameters are
            just the integration parameters with the first num_x columns removed, this is a matrix of shape
            (num_dtheta, num_z - num_x).

        Returns
        -------
        y_dot : np.array
            The rates associated with each state in the augmented state vector (primal and tangent states).
        """
        n_x = self.x_size
        n_theta = self.theta_size
        n_z = n_x + n_theta

        x = y[:n_x].reshape((1, n_x))
        td = theta[1]

        dx_dz = y[n_x:n_x + n_x * n_z].reshape((n_x, n_z))
        dt_dz = y[-n_z:].reshape((1, n_z))

        x_dot, f_x, f_t, f_theta = self.eval_ode(x, t, theta, eval_solution=True, eval_derivs=True)

        dh_dz = np.zeros((1, n_z))
        dh_dz[0, n_x+1] = 1./td
        dt_dz_dot = dh_dz

        dx_dz_dot = f_x @ dx_dz + f_t @ dt_dz + f_theta @ dtheta_dz + x_dot @ dt_dz_dot

        y_dot = np.concatenate((x_dot.ravel(),
                                dx_dz_dot.ravel(),
                                dt_dz_dot.ravel()))

        return y_dot

    def _f_primal(self, t, x, theta):
        """
        The ODE-callable function where y is the augmented state vector, theta are the ODE parameters, and dtheta_dz
        are the sensitivities of the ODE parameters to the integration parameters.

        Parameters
        ----------
        t : float
            The current value of the integration variable.
        y : np.array
            The augmented state vector.
        theta : np.array
            The ODE parameter vector. The first two elements are t_initial and t_duration.

        Returns
        -------
        y_dot : np.array
            The rates associated with each state in the augmented state vector (primal and tangent states).
        """
        x_dot, _, _, _ = self.eval_ode(x, t, theta, eval_solution=True, eval_derivs=False)

        return x_dot

    def _propagate(self, inputs, propagate_derivs=None, x_out=None, t_out=None, dx_dz_out=None,
                   dt_dz_out=None):
        """
        Propagate the augmented state, y = [x, dx_dz, dt_dz].

        Parameters
        ----------
        inputs : Vector
            The inputs of the integration. This is an OpenMDAO vector including the initial state values, t_initial
            and t_duration, the ODE parameters, the control values, and the polynomial control values.
        propagate_derivs : bool or None
            If True, propagate the derivatives of the states and of time w.r.t. the integration parameters vector, z.
            If None, use the value of option `propagate_derivs` to determine if derivatives should be computed.
        x_out : np.array or None.
            Pre-allocated storage for the results of the state propagation. If None, this storage will be allocated
            on each call to _propagate.
        t_out : np.array or None.
            Pre-allocated storage for the time at the requested nodes. If None, this storage will be allocated
            on each call to _propagate.
        dx_dz_out : np.array or None.
            Pre-allocated storage for the results of the propagation of derivatives of the states with respect to the
            integration parameters. If None, this storage will be allocated on each call to _propagate.
        dt_dz_out : np.array or None.
            Pre-allocated storage derivatives of the time at the requested nodes with respect to the integration
            parameters. If None, this storage will be allocated on each call to _propagate.

        Returns
        -------
        x_out : np.array
            The integrated states at each node.
        t_out : np.array
            The time (or integration variable) value at each node.
        dx_dz_out : np.array
            The derivative of each state at each node with respect to the initial integration parameters.
        dt_dz_out : np.array
            The derivative of time at each node with respect to the initial integration parameters.
        """
        method = self.options['method']
        first_step = self.options['first_step']
        max_step = self.options['max_step']
        atol = self.options['atol']
        rtol = self.options['rtol']
        ogd = self._output_grid_data

        nn = sum(self._nnps)
        if x_out is None:
            x_out = np.zeros((nn, self.x_size))
        if t_out is None:
            t_out = np.zeros((nn, 1))

        if propagate_derivs is None:
            _propagate_derivs = self.options['propagate_derivs']
        else:
            _propagate_derivs = propagate_derivs

        n_x = self.x_size
        n_theta = self.theta_size
        n_z = n_x + n_theta
        nnps = self._nnps

        # Extract the input values
        x0 = np.zeros((self.x_size,))
        theta = np.zeros((self.theta_size,))

        for state_name in self.state_options:
            state_initial_val = inputs[self._state_input_names[state_name]]
            x0[self.state_idxs[state_name]] = state_initial_val

        theta[0] = t_initial = inputs['t_initial']
        theta[1] = t_duration = inputs['t_duration']

        for param_name in self.parameter_options:
            param_val = inputs[self._param_input_names[param_name]]
            theta[self._parameter_idxs_in_theta[param_name]] = param_val.ravel()

        for control_name in self.control_options:
            control_vals = inputs[self._control_input_names[control_name]]
            theta[self._control_idxs_in_theta[control_name]] = control_vals.ravel()

        for pc_name in self.polynomial_control_options:
            pc_vals = inputs[self._polynomial_control_input_names[pc_name]]
            theta[self._polynomial_control_idxs_in_theta[pc_name]] = pc_vals.ravel()

        if _propagate_derivs:
            if dx_dz_out is None:
                dx_dz_out = np.zeros((nn, self.x_size * self.z_size))
            if dt_dz_out is None:
                dt_dz_out = np.zeros((nn, self.z_size))

            y0 = np.concatenate((x0.ravel(), self._dx_dz_0.ravel(), self._dt_dz_0.ravel()))
        else:
            dx_dz_out = None
            dt_dz_out = None
            y0 = x0.ravel()

        row_seg_i = 0

        for i in range(ogd.num_segments):
            self._set_segment_index(i)

            eval_nodes_ptau = ogd.node_ptau[ogd.segment_indices[i, 0]: ogd.segment_indices[i, 1]]

            t_eval_seg = t_initial + 0.5 * (eval_nodes_ptau + 1) * t_duration
            t_span_seg = (t_eval_seg[0], t_eval_seg[-1])

            if _propagate_derivs:
                # The augmented initial state vector
                sol = solve_ivp(self._f_augmented, t_span=t_span_seg, t_eval=t_eval_seg, y0=y0,
                                args=(theta, self._dtheta_dz), method=method, first_step=first_step, max_step=max_step,
                                atol=atol, rtol=rtol)
                dx_dz_out[row_seg_i:row_seg_i+nnps[i], :] = sol.y.T[:, n_x:n_x+n_x*n_z]
                dt_dz_out[row_seg_i:row_seg_i+nnps[i], :] = sol.y.T[:, -n_z:]
            else:
                sol = solve_ivp(self._f_primal, t_span=t_span_seg, t_eval=t_eval_seg, y0=y0, args=(theta,),
                                method=method, first_step=first_step, max_step=max_step, atol=atol, rtol=rtol)

            x_out[row_seg_i:row_seg_i+nnps[i], :] = sol.y.T[:, :n_x]  # Save solution to the output nodes
            t_out[row_seg_i:row_seg_i + nnps[i], 0] = sol.t
            y0 = sol.y.T[-1, :]  # Set initial y for the next segment
            row_seg_i += nnps[i]  # Increment node associated with the start of the next segment

        return x_out, t_out, dx_dz_out, dt_dz_out

    def compute(self, inputs, outputs):
        """
        Compute propagated state values.

        Parameters
        ----------
        inputs : `Vector`
            `Vector` containing inputs.
        outputs : `Vector`
            `Vector` containing outputs.
        """
        t_name = self.time_options['name']

        inputs_hash = inputs.get_hash()
        if inputs_hash != self._inputs_cache:
            self._propagate(inputs=inputs, propagate_derivs=self.options['propagate_derivs'],
                            x_out=self._x_out, t_out=self._t_out, dx_dz_out=self._dx_dz_out, dt_dz_out=self._dt_dz_out)

            self._inputs_cache = inputs_hash

        t = self._t_out
        x = self._x_out

        # Extract time
        outputs[t_name] = t
        outputs[f'{t_name}_phase'] = t - inputs['t_initial']
        outputs['t_final'] = inputs['t_initial'] + inputs['t_duration']

        # Extract the state values
        for state_name in self.state_options:
            of = self._state_output_names[state_name]
            outputs[of] = x[:, self.state_idxs[state_name]]

    def compute_partials(self, inputs, partials):
        """
        Compute derivatives of propagated states wrt the inputs.

        Parameters
        ----------
        inputs : Vector
            Unscaled, dimensional input variables read via inputs[key].
        partials : Jacobian
            Subjac components written to partials[output_name, input_name].
        """
        # Only propagate the ODE if our inputs have changed, otherwise use the cached outputs.
        inputs_hash = inputs.get_hash()
        if inputs_hash != self._inputs_cache:
            self._propagate(inputs=inputs, propagate_derivs=True,
                            x_out=self._x_out, t_out=self._t_out, dx_dz_out=self._dx_dz_out, dt_dz_out=self._dt_dz_out)
            self._inputs_cache = inputs_hash

        t_name = self.time_options['name']

        dt_dz = self._dt_dz_out
        dx_dz = self._dx_dz_out

        partials[t_name, 't_duration'] = dt_dz[:, self.x_size+1]
        partials[f'{t_name}_phase', 't_duration'] = dt_dz[:, self.x_size+1]

        for state_name in self.state_options:
            of = self._state_output_names[state_name]

            for wrt_state_name in self.state_options:
                wrt = self._state_input_names[wrt_state_name]
                partials[of, wrt] = dx_dz[self._partial_dx_dz_idxs[of, wrt]]

            partials[of, 't_initial'] = dx_dz[self._partial_dx_dz_idxs[of, 't_initial']]
            partials[of, 't_duration'] = dx_dz[self._partial_dx_dz_idxs[of, 't_duration']]

            for wrt_param_name in self.parameter_options:
                wrt = self._param_input_names[wrt_param_name]
                partials[of, wrt] = dx_dz[self._partial_dx_dz_idxs[of, wrt]]

            for wrt_control_name in self.control_options:
                wrt = self._control_input_names[wrt_control_name]
                partials[of, wrt] = dx_dz[self._partial_dx_dz_idxs[of, wrt]]

            for wrt_pc_name in self.polynomial_control_options:
                wrt = self._polynomial_control_input_names[wrt_pc_name]
                partials[of, wrt] = dx_dz[self._partial_dx_dz_idxs[of, wrt]]