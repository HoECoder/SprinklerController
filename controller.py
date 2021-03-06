import time
from copy import deepcopy
import logging
import controller_settings
from dispatchers import TestDispatcher

interval_types = ["even", "odd", "day_of_week"]

def monkey_program(program, time_delta=10):
    n = make_now()
    even_odd = {1 : "odd",
                0 : "even"}[n["day"] % 2]
    program[controller_settings.INTERVAL_KEY] = {"type" : even_odd}
    program[controller_settings.TIME_OF_DAY_KEY] = n["seconds_from_midnight"] + time_delta

def make_now():
    # We build now (year,month,day,day_of_week,hour,minute,second,seconds_from_midnight)
    current_time = time.localtime()
    n = dict()
    n["year"] = current_time.tm_year
    n["month"] = current_time.tm_mon
    n["day"] = current_time.tm_mday
    n["day_of_week"] = current_time.tm_wday
    n["hour"] = current_time.tm_hour
    n["minute"] = current_time.tm_min
    n["second"] = current_time.tm_sec

    hrs = n["hour"] * 3600
    mins = n["minute"] * 60
    secs = n["second"]

    seconds = hrs + mins + secs

    n["seconds_from_midnight"] = seconds

    return n

def within_program_time(program, clock):
    start_time = program[controller_settings.TIME_OF_DAY_KEY]
    duration = program[controller_settings.TOTAL_RUN_TIME_KEY]
    end_time = start_time+duration
    return start_time <= clock and clock < end_time

def is_program_run_day(program, now):
    # program_id is the program we would like to check
    # now has fields "year", "month", "day", "day_of_week", "hour", "minute", "second", "seconds_from_midnight"
    if program is None:
        return False #should throw an error here
    interval = program[controller_settings.INTERVAL_KEY]
    interval_type = interval[controller_settings.INTERVAL_TYPE_KEY]
    if interval_type in controller_settings.odd_even_types: # Run on even or odd days
        day = now["day"]
        even_odd = day % 2 == 0
        if interval_type == controller_settings.EVEN_INTERVAL_TYPE:
            return even_odd
        else:
            return not even_odd
    elif interval_type == controller_settings.DOW_INTERVAL_TYPE:
        # Day of week is a number in the set (0-6) (i.e. Mon-Sun)
        # We see if this is in the intervals list of days
        wd = now["day_of_week"]
        run_days = interval.get(controller_settings.RUN_DAYS_KEY, None)
        if run_days is None:
            return False # We should throw an error here too
        return wd in run_days
    else:
        return False # should throw another error here
    return False

def asses_program(program, clock, now):
    do_append = False
    if program[controller_settings.IN_PROGRAM_KEY]: # Grab a program that may already be running
        do_append = True
        # Check if we should expire this program
        if not within_program_time(program, clock):
            program["expire"] = True # We should expire this program
        if not is_program_run_day(program, now):
            program["expire"] = True # On the chance we got suspended and the day changed on us
    else: # Grab programs that should be running
        if is_program_run_day(program, now): # We look if it is a run day
            if within_program_time(program, clock): # We should run this program
                do_append = True
    return do_append

def _prepare_program(program):
    total_run_time = 0
    for sd in program[controller_settings.STATION_DURATION_KEY]:
        tod = program[controller_settings.TIME_OF_DAY_KEY]
        station_run = sd[controller_settings.DURATION_KEY]
        sd["start_time"] = total_run_time + tod
        total_run_time = total_run_time + station_run
        sd["end_time"] = total_run_time + tod
        #sd["running"] = False
    program[controller_settings.TOTAL_RUN_TIME_KEY] = total_run_time
    return program

class Controller(object):
    def __init__(self, dispatcher_class=TestDispatcher, settings=None):
        self.logger = logging.getLogger(__name__)
        self.programs = None
        if settings is None:
            self.settings = controller_settings.ControllerSettings()
        if not self.settings.load_master():
            self.settings.master_settings = controller_settings.default_master
        self.settings.get_programs()
        self.programs = self.settings.programs
        self.tickover = 0
        # Class to buffer us from the device. Either this is real HW or a dummy that just logs
        self.dispatcher = dispatcher_class()
        #Set full stop pattern
        total_stations = self.settings.master_settings[controller_settings.STATIONS_AVAIL_KEY]
        self.full_stop_pattern = [0 for x in xrange(total_stations)]
        self.master_pattern = list(self.full_stop_pattern)
        # One shot program is used to either run a full program or just a station
        self.one_shot_program = None
    def prepare_programs(self):
        for program in self.programs.values():
            _prepare_program(program)

    def get_current_programs(self, now):
        running_programs = list()
        clock = now["seconds_from_midnight"]
        for program in self.programs.values():
            do_append = asses_program(program, clock, now)
            if do_append:
                running_programs.append(program)
        # We allow users to run a single program once
        if not self.one_shot_program is None:
            do_append = asses_program(self.one_shot_program, clock, now)
            if do_append:
                running_programs.append(self.one_shot_program)
        return running_programs

    def stop_program(self, program_id):
        if program_id == -1:
            self.logger.info("Stopping one shot program")
            self.dispatch_full_stop()
            self.one_shot_program = None
            return
        program = self.programs.get(program_id, None)
        if program is None:
            return
        program[controller_settings.IN_PROGRAM_KEY] = False
        program.pop("expire", None)
        for station in program[controller_settings.STATION_DURATION_KEY]:
            station[controller_settings.IN_STATION_KEY] = False
        self.logger.info("Stopping program: %d", program_id)
        self.dispatch_full_stop()
    def start_program(self, program_id, now):
        if program_id == -1:
            program = self.one_shot_program
        else:
            program = self.programs.get(program_id, None)
        if program is None:
            return
        program[controller_settings.IN_PROGRAM_KEY] = True
        self.logger.info("Starting program: %d", program_id)
        self.advance_program(program_id, now)
    def add_one_shot_program(self, program_id):
        # we clone the original program
        original_program = self.programs.get(program_id, None)
        if original_program is None:
            return
        new_program = deepcopy(original_program)
        new_program[controller_settings.PROGRAM_ID_KEY] = -1
        monkey_program(new_program, time_delta=2)
        _prepare_program(new_program) # reset the program
        if not self.one_shot_program is None:
            self.stop_program(-1)
        self.one_shot_program = new_program
    def add_single_station_program(self, station_id, duration):
        program = deepcopy(controller_settings.station_template)
        sd = deepcopy(controller_settings.station_duration_template)
        program[controller_settings.STATION_DURATION_KEY].append(sd)
        sd[controller_settings.STATION_ID_KEY] = station_id
        sd[controller_settings.DURATION_KEY] = duration
        monkey_program(program, time_delta=2)
        _prepare_program(program)
        if not self.one_shot_program is None:
            self.stop_program(-1)
        self.one_shot_program = program
    def add_new_program(self, program):
        program[controller_settings.PROGRAM_ID_KEY] = -2
        if self.settings.add_new_program(program):
            _prepare_program(program)
            self.programs = self.settings.programs
            # TODO alert on the return value
    def is_station_available(self, stid):
        master = self.settings.master_settings
        station_list = master[controller_settings.STATION_LIST_KEY]
        station = station_list.get(stid, None)
        if station is None:
            return False
        wired = station[controller_settings.WIRED_KEY]
        self.logger.debug("Station %d is wired: %s", stid, str(wired))
        return wired
    def advance_program(self, program_id, now):
        if program_id == -1:
            program = self.one_shot_program
        else:
            program = self.programs.get(program_id, None)
        if program is None:
            return
        clock = now["seconds_from_midnight"]
        start_time = program[controller_settings.TIME_OF_DAY_KEY]
        elapsed_time = clock - start_time
        #run_length = 0
        stop_stations = list()
        start_stations = list()
        self.logger.debug("Checking advancement. Clock: %d, start: %d, elapsed: %d",
                          clock,
                          start_time,
                          elapsed_time)
        # We go through all the stations in the program
        # We determine who needs to start and stop
        for station in program[controller_settings.STATION_DURATION_KEY]:
            station_start = station["start_time"]
            station_stop = station["end_time"]
            running = station[controller_settings.IN_STATION_KEY]
            stid = station[controller_settings.STATION_ID_KEY]
            self.logger.debug("Station stid:%d, start %d, stop %d, running %s",
                              stid,
                              station_start,
                              station_stop,
                              str(running))
            if  station_start <= clock and clock < station_stop:
                if not running:
                    # Fire up the station
                    self.logger.debug("Fire up the station: %d", stid)
                    start_stations.append(stid)
                    station[controller_settings.IN_STATION_KEY] = True
                else:
                    self.logger.debug("Station is already running: %d", stid)
                    #Otherwise we sit patiently. Latching relays
            else: #Station is old
                if running: # We have to stop this guy first
                    stop_stations.append(stid)
                    station[controller_settings.IN_STATION_KEY] = False
                    self.logger.debug("Stopping station: %d", stid)
                else:
                    self.logger.debug("Station was not running: %d", stid)
        # Now we stop all stations first
        self.dispatch_stop(stop_stations)
        # Now we start all stations
        self.dispatch_start(start_stations)
    def dispatch_full_stop(self):
        self.logger.debug("Dispatch FULL STOP")
        self.dispatcher.write_pattern_to_register(self.full_stop_pattern)
        self.master_pattern = list(self.full_stop_pattern)
        self.logger.info("Full stop complete")
    def dispatch_stop(self, stations):
        self.logger.debug("Stopping stations: %s", str(stations))
        for station in stations:
            self.master_pattern[station-1] = 0
        if len(stations) > 0:
            self.logger.debug("Pattern : %s", str(self.master_pattern))
            self.dispatcher.write_pattern_to_register(self.master_pattern)
        self.logger.info("Stopped stations: %s", str(stations))
    def dispatch_start(self, stations):
        self.logger.debug("Starting stations: %s", str(stations))
        for station in stations:
            st_avail = self.is_station_available(station)
            self.logger.debug("Station %d is enabled: %s", station, str(st_avail))
            if st_avail:
                self.logger.debug("Station %d (%d) on", station, station-1)
                self.master_pattern[station-1] = 1
        if len(stations) > 0:
            self.logger.debug("Pattern : %s", str(self.master_pattern))
            self.dispatcher.write_pattern_to_register(self.master_pattern)
        self.logger.info("Started stations: %s", str(stations))
    def tick(self):
        # This is our main function. Should be called from some sort of loop
        # 1. We build now (year,month,day,day_of_week,hour,minute,second,seconds_from_midnight)
        # 2. We find any running programs
        # 3. Loop over the programs (including the one_shot_program)
        # 3.a If the program is expired, stop it
        # 3.b If a program is live, possibly advance its stations
        # 3.c If a new program is up, start it
        # 4. Periodically persist settings and programs

        # 1. Build NOW
        n = make_now()
        self.logger.debug("Now: %s", str(n))
        self.logger.debug("Getting programs")
        # 2. Get the list of programs
        running_programs = self.get_current_programs(n)
        # 3. Loop over the programs
        if len(running_programs) > 0:
            self.logger.info("Checking programs")
        else:
            self.logger.debug("No programs this tick")
        for program in running_programs:
            # 3.a Expire the expired programs
            expired = program.get("expire", False)
            in_program = program.get(controller_settings.IN_PROGRAM_KEY, None)
            pid = program[controller_settings.PROGRAM_ID_KEY]
            if expired:
                self.logger.info("Expiring progam: %d", pid)
                self.stop_program(pid)
            elif in_program: # 3.b Possibly advance the program
                self.logger.debug("Checking for advancement of: %d", pid)
                self.advance_program(pid, n)
            else: # 3.c Start up the program
                self.logger.info("Starting up program: %d", pid)
                self.start_program(pid, n)
        # Push out settings
        if self.tickover % 5 == 0:
            self.settings.dump_master()
            self.settings.dump_all_programs()
        self.tickover = self.tickover + 1
