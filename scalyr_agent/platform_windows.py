# Copyright 2014 Scalyr Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------------------------------------------------
#
# author: Scott Sullivan <guy.hoozdis@gmail.com>

__author__ = 'guy.hoozdis@gmail.com'

# TODO:
#  * Control flow (agent_run_method returns => stop service)
#  X register_for_termination() - test the code Steven and I wrote
#  X register_for_status_request() - imitate the termination handler example
#  X request_agent_status() - controller method required for register_for_status_request()
#  X stop_agent_service() - implement real code
#  X start_agent_service() - implement real code

import sys

try:
    from scalyr_agent.platform_controller import PlatformController, DefaultPaths, AgentAlreadyRunning
except ImportError:
    # The module lookup path list might fail when this module is being hosted by
    # PythonService.exe, so append the lookup path and try again.
    from os import path
    sys.path.append(
        path.dirname(
            path.dirname(
                path.abspath(__file__)
            )
        )
    )
    from scalyr_agent.platform_controller import PlatformController, DefaultPaths, AgentAlreadyRunning

from __scalyr__ import get_install_root

import win32serviceutil
import win32service
import servicemanager
import win32event
import win32api


def QueryServiceStatusEx(servicename):
    hscm = win32service.OpenSCManager(None, None, win32service.SC_MANAGER_CONNECT)
    try:
        hs = win32serviceutil.SmartOpenService(hscm, servicename, win32service.SERVICE_QUERY_STATUS)
        try:
            status = win32service.QueryServiceStatusEx(hs)
        finally:
            win32service.CloseServiceHandle(hs)
    finally:
        win32service.CloseServiceHandle(hscm)
    return status


class ScalyrService(win32serviceutil.ServiceFramework):
    _svc_name_ = "ScalyrAgent"
    _svc_display_name_ = "Scalyr Agent"
    _svc_description_ = "Hosts Scalyr metric collectors"

    SERVICE_CONTROL_SCALYR = 16

    def __init__(self, *args):
        win32serviceutil.ServiceFramework.__init__(self, *args)
        self.log('Instantiate scalyr service')
        self._stop_event = win32event.CreateEvent(None, 0, 0, None)

    def log(self, msg):
        servicemanager.LogInfoMsg(msg)

    def sleep(self, sec):
        win32api.Sleep(sec*1000, True)

    def SvcStop(self):
        self.log('Stopping scalyr service')
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self._stop_event)
        self.controller.invoke_termination_handler()
        self.ReportServiceStatus(win32service.SERVICE_STOPPED)
        self.log('Stopped scalyr service')

    def SvcDoRun(self):
        self.ReportServiceStatus(win32service.SERVICE_START_PENDING)
        try:
            self.ReportServiceStatus(win32service.SERVICE_RUNNING)
            self.log('Starting service')
            self.start()
            self.log('Waiting for stop event')
            win32event.WaitForSingleObject(self._stop_event, win32event.INFINITE)
            self.log('Stop event triggered')
        except Exception, e:
            self.log('ERROR: {}'.format(e))
            self.SvcStop()

    def start(self):
        from scalyr_agent.agent_main import ScalyrAgent, create_commandline_parser
        from scalyr_agent.platform_controller import PlatformController

        self.controller = PlatformController.new_platform()
        parser = create_commandline_parser()
        self.controller.add_options(parser)
        options, args = parser.parse_args(['start'])
        self.controller.consume_options(options)

        self.log("Calling agent_run_method()")
        agent = ScalyrAgent(self.controller)
        agent.agent_run_method(self.controller, options.config_filename)
        self.log("Exiting agent_run_method()")

    def SvcOther(self, control):
        self.log('SvcOther (control=%d)' % control)
        if 128 == control:
            self.log('invoking status handler')
            self.controller.invoke_status_handler()
            self.log('status handler invoked')
        else:
            self.log('propigating control code=%d' % control)
            win32serviceutil.ServiceFramework.SvcOther(self, control)
        self.log('Exiting SvcOther')
    
class WindowsPlatformController(PlatformController):
    """A controller instance for Microsoft's Windows platforms
    """

    def invoke_termination_handler(self):
        if self.__termination_handler:
            self.__termination_handler()

    def invoke_status_handler(self):
        if self.__status_handler:
            self.__status_handler()

    def can_handle_current_platform(self):
        """Returns true if this platform object can handle the server this process is running on.

        @return: True if this platform instance can handle the current server.
        @rtype: bool
        """
        return 'win32' == sys.platform

    @property
    def default_paths(self):
        """Returns the default paths to use for various configuration options for this platform.

        @return: The default paths
        @rtype: DefaultPaths
        """
        # NOTE: For this module, it is assumed that the 'install_type' is always PACKAGE_INSTALL
        # TODO: These are not ideal paths, just something to get us started.
        return DefaultPaths(
                r'\Temp\scalyr\log',
                r'\Temp\scalyr\agent.json',
                r'\Temp\scalyr\lib')

    def run_as_user(self, user_id, script_file, script_arguments):
        """Restarts this process with the same arguments as the specified user.

        This will re-run the entire Python script so that it is executing as the specified user.
        It will also add in the '--no-change-user' option which can be used by the script being executed with the
        next proces that it was the result of restart so that it probably shouldn't do that again.

        @param user_id: The user id to run as, typically 0 for root.
        @param script_file: The path to the Python script file that was executed.
        @param script_arguments: The arguments passed in on the command line that need to be used for the new
            command line.

        @type user_id: int
        @type script_file: str
        @type script_arguments: list<str>
        """
        # TODO: Selects user based on owner of the config file.  Do we want to do the same thing on this platform?
        pass

    def is_agent_running(self, fail_if_running=False):
        """Returns true if the agent service is running, as determined by this platform implementation.

        This will optionally raise an Exception with an appropriate error message if the agent is not running.

        @param fail_if_running:  True if the method should raise an Exception with a platform-specific error message
            explaining how it determined the agent is not running.
        @type fail_if_running: bool

        @return: True if the agent process is already running.
        @rtype: bool

        @raise AgentAlreadyRunning: If the agent is running and fail_if_running is True.
        """
        status = QueryServiceStatusEx(ScalyrService._svc_name_)
        state = status['CurrentState']

        if fail_if_running and state in (win32service.SERVICE_RUNNING, win32service.SERVICE_START_PENDING):
            pid = status['ProcessId']
            raise AgentAlreadyRunning('The agent appears to be running pid=%d' % pid) 

        return state in (win32service.SERVICE_RUNNING, win32service.SERVICE_START_PENDING)

    def start_agent_service(self, agent_run_method, quiet):
        """Start the agent service using the platform-specific method.

        This method must return once the agent service has been started.

        @param agent_run_method: The method to invoke to actually run the agent service.  This method takes one
            argument, the reference to this controller.  Note, if your platform implementation cannot use this
            function pointer (because the service is running in a separate address space and cannot be passed this
            pointer), then instead of invoking this method, you may invoke ScalyrAgent.agent_run_method instead.
        @param quiet: True if only error messages should be printed to stdout, stderr.

        @type agent_run_method: func(PlatformController)
        @type quiet: bool
        """
        # TODO: Add Error handling.  Insufficient privileges will raise an exception
        # that is currently unhandled.
        win32serviceutil.StartService(ScalyrService._svc_name_)

    def stop_agent_service(self, quiet):
        """Stops the agent service using the platform-specific method.

        This method must return only after the agent service has terminated.

        @param quiet: True if only error messages should be printed to stdout, stderr.
        @type quiet: bool
        """
        # TODO: Add Error handling. Trying to stop a service that isn't running
        # will raise an exception that is currently unhandled.
        win32serviceutil.StopService(ScalyrService._svc_name_)

    def get_usage_info(self):
        """Returns CPU and memory usage information.

        It returns the results in a tuple, with the first element being the number of
        CPU seconds spent in user land, the second is the number of CPU seconds spent in system land,
        and the third is the current resident size of the process in bytes."""
        # TODO: Implement the data structure (cpu, sys, phy_ram)
        return (0, 0, 0)

    def register_for_termination(self, handler):
        """Register a method to be invoked if the agent service is requested to terminated.

        This should only be invoked by the agent service once it has begun to run.

        @param handler: The method to invoke when termination is requested.
        @type handler:  func
        """
        self.__termination_handler = handler

    def register_for_status_requests(self, handler):
        """Register a method to be invoked if this process is requested to report its status.

        This is used to implement the 'scalyr-agent-2 status -v' feature.

        This should only be invoked by the agent service once it has begun to run.

        @param handler:  The method to invoke when status is requested.
        @type handler: func
        """
        self.__status_handler = handler

    def request_agent_status(self):
        """Invoked by a process that is not the agent to request the current agent dump the current detail
        status to the status file.

        This is used to implement the 'scalyr-agent-2 status -v' feature.

        @return: If there is an error, an errno that describes the error.  errno.EPERM indicates the current does not
            have permission to request the status.  errno.ESRCH indicates the agent is not running.
        """
        win32serviceutil.ControlService(ScalyrService._svc_name_, 128)


if __name__ == "__main__":
    sys.exit(
        win32serviceutil.HandleCommandLine(ScalyrService)
    )
