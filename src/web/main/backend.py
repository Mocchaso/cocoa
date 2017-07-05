import json

__author__ = 'anushabala'
import uuid
from src.web.main.web_states import FinishedState, UserChatState, WaitingState, SurveyState
from src.turk.accept_negotiation_hits import reject_transcript
from src.basic.systems.human_system import HumanSystem
from src.scripts.visualize_data import visualize_chat
from src.web.dump_events_to_json import convert_events_to_json
import hashlib
import sqlite3
import time
import numpy as np
from src.basic.controller import Controller
from src.basic.event import Event
from flask import Markup
from uuid import uuid4
from collections import defaultdict
from backend_utils import Status, UnexpectedStatusException, ConnectionTimeoutException, \
    StatusTimeoutException, NoSuchUserException, Messages, current_timestamp_in_seconds, User
from web_logger import WebLogger

m = hashlib.md5()
m.update("bot")


class BackendConnection(object):
    @staticmethod
    def get_backend(*args):
        import src.config as config
        if config.task == config.MutualFriends:
            return MutualFriendsBackend(*args)
        elif config.task == config.Negotiation:
            return NegotiationBackend(*args)
        else:
            raise ValueError('[Backend] Unknown task: %s' % config.task)


class BaseBackend(object):
    def __init__(self, params, schema, scenario_db, systems, sessions, controller_map, pairing_probabilities):
        self.config = params
        self.conn = sqlite3.connect(params["db"]["location"])

        self.do_survey = True if "end_survey" in params.keys() and params["end_survey"] == 1 else False
        self.scenario_db = scenario_db
        self.schema = schema
        self.systems = systems
        self.sessions = sessions
        self.controller_map = controller_map
        self.pairing_probabilities = pairing_probabilities
        self.logger = WebLogger.get_logger()

    def _update_user(self, cursor, userid, **kwargs):
        if "status" in kwargs:
            kwargs["status_timestamp"] = current_timestamp_in_seconds()
        if "connected_status" in kwargs and "connected_timestamp" not in kwargs:
            kwargs["connected_timestamp"] = current_timestamp_in_seconds()
        keys = sorted(kwargs.keys())
        values = [kwargs[k] for k in keys]
        set_string = ", ".join(["{}=?".format(k) for k in keys])

        cursor.execute("UPDATE active_user SET {} WHERE name=?".format(set_string), tuple(values + [userid]))

    def _get_session(self, userid):
        return self.sessions.get(userid)

    def _stop_waiting_and_transition_to_finished(self, cursor, userid):
        self._update_user(cursor, userid,
                          status=Status.Finished,
                          message=Messages.WaitingTimeExpired)

    def _end_chat(self, cursor, userid):
        def _update_scenario_db():
            u = self._get_user_info_unchecked(cursor, userid)
            sid = controller.scenario.uuid
            partner_type = u.partner_type
            chat_id = controller.get_chat_id()
            self.decrement_active_chats(cursor, sid, partner_type, chat_id)

        controller = self.controller_map[userid]
        outcome = controller.get_outcome()
        self.update_chat_reward(cursor, controller.get_chat_id(), outcome)
        _update_scenario_db()
        self.logger.debug("Setting controller for chat {:s} to inactive".format(controller.get_chat_id()))
        controller.set_inactive()
        # self.controller_map[userid] = None

    def _ensure_not_none(self, v, exception_class):
        if v is None:
            raise exception_class()
        else:
            return v

    @staticmethod
    def _is_timeout(timeout_limit, timestamp):
        if timeout_limit < 0:
            return False
        num_seconds_remaining = (timeout_limit + timestamp) - current_timestamp_in_seconds()
        return num_seconds_remaining <= 0

    def _assert_no_connection_timeout(self, connection_status, connection_timestamp):
        if connection_status == 1:
            if self._is_timeout(self.config["idle_timeout_num_seconds"], connection_timestamp):
                raise ConnectionTimeoutException()
            else:
                return
        else:
            if self._is_timeout(self.config["connection_timeout_num_seconds"], connection_timestamp):
                raise ConnectionTimeoutException()
            else:
                return

    def _assert_no_status_timeout(self, status, status_timestamp):
        N = self.config["status_params"][status]["num_seconds"]
        if N < 0:  # don't timeout for some statuses
            return

        if self._is_timeout(N, status_timestamp):
            raise StatusTimeoutException()
        else:
            return

    @staticmethod
    def _validate_status_or_throw(assumed_status, status):
        if status != assumed_status:
            raise UnexpectedStatusException(status, assumed_status)
        return

    @staticmethod
    def _generate_chat_id():
        return "C_" + uuid4().hex

    def _get_user_info(self, cursor, userid, assumed_status=None):
        u = self._get_user_info_unchecked(cursor, userid)
        if assumed_status is not None:
            self._validate_status_or_throw(assumed_status, u.status)
        try:
            self._assert_no_connection_timeout(u.connected_status, u.connected_timestamp)
        except ConnectionTimeoutException:
            self.logger.debug("User {:s} had connection timeout exception with connected status "
                              "{:d}".format(userid, u.connected_status))
            self._update_user(cursor, userid, connected_status=0, connected_timestamp=u.connected_timestamp)
            raise ConnectionTimeoutException
        self._assert_no_status_timeout(u.status, u.status_timestamp)
        return u

    def _get_user_info_unchecked(self, cursor, userid):
        cursor.execute("SELECT * FROM active_user WHERE name=?", (userid,))
        x = cursor.fetchone()
        u = User(self._ensure_not_none(x, NoSuchUserException))
        return u

    def add_chat_to_db(self, chat_id, scenario_id, agent0_id, agent1_id, agent0_type, agent1_type):
        agents = json.dumps({0: agent0_type, 1: agent1_type})
        agent_ids = json.dumps({0: agent0_id, 1: agent1_id})
        now = str(time.time())
        with self.conn:
            cursor = self.conn.cursor()
            cursor.execute('''INSERT INTO chat VALUES (?,?,"",?,?,?)''', (chat_id, scenario_id, agent_ids, agents, now))

    def add_event_to_db(self, chat_id, event):
        def _create_row(chat_id, event):
            data = event.data
            if event.action == 'select':
                data = json.dumps(event.data)
            if event.action == 'offer':
                data = json.dumps(event.data)
            return chat_id, event.action, event.agent, event.time, data, event.start_time

        try:
            with self.conn:
                cursor = self.conn.cursor()
                row = _create_row(chat_id, event)

                cursor.execute('''INSERT INTO event VALUES (?,?,?,?,?,?)''', row)
        except sqlite3.IntegrityError:
            print("WARNING: Rolled back transaction")

    def attempt_join_chat(self, userid):
        def _init_controller(my_index, partner_type, scenario, chat_id):
            my_session = self.systems[HumanSystem.name()].new_session(my_index, scenario.get_kb(my_index))
            partner_session = self.systems[partner_type].new_session(1 - my_index, scenario.get_kb(1 - my_index))

            controller = Controller.get_controller(scenario, [my_session, partner_session], chat_id=chat_id,
                                                   debug=False)

            return controller, my_session, partner_session

        def _pair_with_human(cursor, userid, my_index, partner_id, scenario, chat_id):
            controller, my_session, partner_session = _init_controller(my_index, HumanSystem.name(), scenario, chat_id)
            self.controller_map[userid] = controller
            self.controller_map[partner_id] = controller

            self.sessions[userid] = my_session
            self.sessions[partner_id] = partner_session

            # ensures that partner is actually in waiting state
            self._get_user_info(cursor, partner_id, assumed_status=Status.Waiting)

            self._update_user(cursor, partner_id,
                              status=Status.Chat,
                              partner_id=userid,
                              partner_type=HumanSystem.name(),
                              scenario_id=scenario.uuid,
                              agent_index=1 - my_index,
                              message="",
                              chat_id=chat_id)

            self._update_user(cursor, userid,
                              status=Status.Chat,
                              partner_id=partner_id,
                              partner_type=HumanSystem.name(),
                              scenario_id=scenario_id,
                              agent_index=my_index,
                              message="",
                              chat_id=chat_id)

            return True

        def _pair_with_bot(cursor, userid, my_index, bot_type, scenario, chat_id):
            controller, my_session, bot_session = _init_controller(my_index, bot_type, scenario, chat_id)
            self.controller_map[userid] = controller

            self.sessions[userid] = my_session

            self._update_user(cursor, userid,
                              status=Status.Chat,
                              partner_id=0,
                              partner_type=bot_type,
                              scenario_id=scenario_id,
                              agent_index=my_index,
                              message="",
                              chat_id=chat_id)

            return True

        def _get_other_waiting_users(cursor, userid):
            cursor.execute("SELECT name FROM active_user WHERE name!=? AND status=? AND connected_status=1",
                           (userid, Status.Waiting))
            userids = [r[0] for r in cursor.fetchall()]
            return userids

        def _choose_scenario_and_partner_type(cursor):
            # for each scenario, get number of complete dialogues per agent type
            all_partners = self.systems.keys()
            cursor.execute('''SELECT * FROM scenario''')
            db_scenarios = cursor.fetchall()
            scenario_dialogues = defaultdict(lambda: defaultdict(int))

            for (scenario_id, partner_type, complete, active) in db_scenarios:
                complete = set(json.loads(complete))
                active = set(json.loads(active))
                # map from scenario ID -> partner type -> # of completed dialogues with that partner
                if scenario_id not in scenario_dialogues:
                    scenario_dialogues[scenario_id] = {}

                scenario_dialogues[scenario_id][partner_type] = len(complete) + len(active)

            # find "active" scenarios (scenarios for which at least one agent type has no completed or active dialogues)
            active_scenarios = defaultdict(list)
            for sid in scenario_dialogues.keys():
                for partner_type in all_partners:
                    if scenario_dialogues[sid][partner_type] == 0:
                        active_scenarios[sid].append(partner_type)

            # if all scenarios have at least one dialogue per agent type (i.e. no active scenarios),
            # just select a random scenario and agent type
            if len(active_scenarios.keys()) == 0:
                sid = np.random.choice(scenario_dialogues.keys())
                p = np.random.choice(all_partners)
                return self.scenario_db.get(sid), p

            # otherwise, select a random active scenario and an agent type that it's missing
            sid = np.random.choice(active_scenarios.keys())
            p = np.random.choice(active_scenarios[sid])
            return self.scenario_db.get(sid), p

        def _update_used_scenarios(scenario_id, partner_type, chat_id):
            cursor.execute(
                '''SELECT active FROM scenario WHERE scenario_id=? AND partner_type=?''',
                (scenario_id, partner_type))
            active_set = set(json.loads(cursor.fetchone()[0]))
            active_set.add(chat_id)
            cursor.execute('''UPDATE scenario SET active=? WHERE scenario_id=? AND partner_type=?''',
                           (json.dumps(list(active_set)), scenario_id, partner_type))

        try:
            with self.conn:
                cursor = self.conn.cursor()
                others = _get_other_waiting_users(cursor, userid)

                my_index = np.random.choice([0, 1])
                scenario, partner_type = _choose_scenario_and_partner_type(cursor)
                scenario_id = scenario.uuid
                chat_id = self._generate_chat_id()
                if partner_type == HumanSystem.name():
                    if len(others) == 0:
                        return None
                    partner_id = np.random.choice(others)

                    try:
                        _pair_with_human(cursor, userid, my_index, partner_id, scenario, chat_id)
                    except UnexpectedStatusException:
                        self.logger.warn("Attempt to pair user {:s} with {:s} failed. User {:s} not in waiting "
                                         "status".format(userid, partner_id, partner_id))
                        return False
                    except ConnectionTimeoutException:
                        self.logger.warn("Attempt to pair user {:s} with {:s} failed. User {:s} had connection "
                                         "timeout".format(userid, partner_id, partner_id))
                        return False
                    _update_used_scenarios(scenario_id, HumanSystem.name(), chat_id)
                    if my_index == 0:
                        self.add_chat_to_db(chat_id, scenario_id, userid, partner_id, HumanSystem.name(),
                                            HumanSystem.name())
                    else:
                        self.add_chat_to_db(chat_id, scenario_id, partner_id, userid, HumanSystem.name(),
                                            HumanSystem.name())
                    self.logger.debug("Paired users {:s} and {:s} in chat with ID {:s} and scenario {:s}".format(
                        userid, partner_id, chat_id, scenario_id
                    ))
                    return True
                else:
                    _update_used_scenarios(scenario_id, partner_type, chat_id)
                    if my_index == 0:
                        self.add_chat_to_db(chat_id, scenario_id, userid, 0, HumanSystem.name(), partner_type)
                    else:
                        self.add_chat_to_db(chat_id, scenario_id, 0, userid, partner_type, HumanSystem.name())

                    self.logger.debug("Paired user {:s} with bot of type {:s} in chat with ID {:s} and scenario "
                                      "{:s}".format(userid, partner_type, chat_id, scenario_id))
                    return _pair_with_bot(cursor, userid, my_index, partner_type, scenario, chat_id)

        except sqlite3.IntegrityError:
            print("WARNING: Rolled back transaction")

    def decrement_active_chats(self, cursor, scenario_id, partner_type, chat_id):
        cursor.execute('''SELECT active FROM scenario WHERE scenario_id=? AND partner_type=?''',
                       (scenario_id, partner_type))
        active_set = set(json.loads(cursor.fetchone()[0]))
        if chat_id in active_set:
            active_set.remove(chat_id)

        cursor.execute(
            '''UPDATE scenario SET active=? WHERE scenario_id=? AND partner_type=?''',
            (json.dumps(list(active_set)), scenario_id, partner_type)
        )

    def user_finished(self, cursor, userid, message=Messages.ChatCompleted):
        new_status = Status.Survey if self.do_survey else Status.Finished
        self._update_user(cursor, userid,
                          status=new_status,
                          message=message,
                          partner_id=-1)

    def end_chat_and_finish(self, cursor, userid, message=Messages.ChatCompleted):
        self._end_chat(cursor, userid)
        self.user_finished(cursor, userid, message)

    def timeout_chat_and_finish(self, cursor, userid, message=Messages.ChatExpired, partner_id=None):
        self.end_chat_and_finish(cursor, userid, message)
        if partner_id is not None and not self.is_user_partner_bot(cursor, userid):
            self.user_finished(cursor, partner_id, message)

    def end_chat_and_redirect(self, cursor, userid, message):
        self._end_chat(cursor, userid)
        self._update_user(cursor, userid,
                          status=Status.Redirected,
                          connected_status=1,
                          message=message)

    def end_chat_and_transition_to_waiting(self, cursor, userid, message, partner_id=None):
        self._end_chat(cursor, userid)
        self._update_user(cursor, userid,
                          status=Status.Waiting,
                          connected_status=1,
                          message=message)
        if partner_id is not None and not self.is_user_partner_bot(cursor, userid):
            self._update_user(cursor, partner_id,
                              status=Status.Waiting,
                              connected_status=0,
                              message=message)

    def check_game_over_and_transition(self, cursor, userid, partner_id):
        game_over, game_complete = self.is_game_over(userid)
        if game_over:
            msg, partner_msg = self.get_completion_messages(userid)
            self.end_chat_and_finish(cursor, userid, message=msg)
            if not self.is_user_partner_bot(cursor, userid):
                self.user_finished(cursor, partner_id, message=partner_msg)
            return True

        return False

    def close(self):
        self.conn.close()
        self.conn = None

    def connect(self, userid):
        try:
            with self.conn:
                cursor = self.conn.cursor()
                self._update_user(cursor, userid,
                                  connected_status=1)
        except sqlite3.IntegrityError:
            print("WARNING: Rolled back transaction")

    def create_user_if_not_exists(self, username):
        with self.conn:
            cursor = self.conn.cursor()
            now = current_timestamp_in_seconds()
            cursor.execute('''INSERT OR IGNORE INTO active_user VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                           (username, Status.Waiting, now, 0, now, "", "", "", "", -1, -1, ""))

    def disconnect(self, userid):
        try:
            with self.conn:
                cursor = self.conn.cursor()
                self._update_user(cursor, userid,
                                  connected_status=0)

        except sqlite3.IntegrityError:
            print("WARNING: Rolled back transaction")

    def get_chat_info(self, userid, peek=False):
        try:
            with self.conn:
                cursor = self.conn.cursor()
                u = self._get_user_info_unchecked(cursor, userid)
                num_seconds_remaining = (self.config["status_params"]["chat"]["num_seconds"] +
                                         u.status_timestamp) - current_timestamp_in_seconds()
                scenario = self.scenario_db.get(u.scenario_id)
                if peek:
                    return UserChatState(u.agent_index, scenario.uuid, u.chat_id, scenario.get_kb(u.agent_index),
                                         scenario.attributes, num_seconds_remaining, scenario.get_kb(1 - u.agent_index))
                else:
                    return UserChatState(u.agent_index, scenario.uuid, u.chat_id, scenario.get_kb(u.agent_index),
                                         scenario.attributes, num_seconds_remaining)

        except sqlite3.IntegrityError:
            print("WARNING: Rolled back transaction")

    def get_completion_messages(self, userid):
        game_over, game_complete = self.is_game_over(userid)
        if game_complete:
            msg = Messages.get_completed_message()
        else:
            msg = Messages.get_incomplete_message()

        return msg, msg

    def get_finished_info(self, userid, from_mturk=False):
        def _generate_mturk_code(completed=True):
            if completed:
                return "MTURK_TASK_C{}".format(str(uuid.uuid4().hex))
            return "MTURK_TASK_I{}".format(str(uuid.uuid4().hex))

        def _add_finished_task_row(cursor, userid, mturk_code, chat_id):
            cursor.execute('INSERT INTO mturk_task VALUES (?,?,?)',
                           (userid, mturk_code, chat_id))

        def _is_chat_complete(cursor, chat_id):
            cursor.execute('''SELECT outcome FROM chat WHERE chat_id=?''', (chat_id,))
            try:
                result = cursor.fetchone()
                if result is None or len(result) == 0:
                    return False

                try:
                    outcome = json.loads(result[0])
                except ValueError:
                    return False

                if outcome['reward'] is None or outcome['reward'] == 0:
                    return False
                else:
                    return True
            except ValueError:
                return False

        try:
            with self.conn:
                cursor = self.conn.cursor()
                u = self._get_user_info(cursor, userid, assumed_status=Status.Finished)
                num_seconds = (self.config["status_params"]["finished"]["num_seconds"] +
                               u.status_timestamp) - current_timestamp_in_seconds()
                completed = _is_chat_complete(cursor, u.chat_id)
                if from_mturk:
                    mturk_code = _generate_mturk_code(completed)
                    self.logger.debug("User {:s} got completion code {:s}".format(userid, mturk_code))
                else:
                    mturk_code = None
                _add_finished_task_row(cursor, userid, mturk_code, u.chat_id)
                return FinishedState(Markup(u.message), num_seconds, mturk_code)

        except sqlite3.IntegrityError:
            print("WARNING: Rolled back transaction")

    def get_survey_info(self, userid):
        try:
            with self.conn:
                cursor = self.conn.cursor()
                u = self._get_user_info(cursor, userid, assumed_status=Status.Survey)
                scenario = self.scenario_db.get(u.scenario_id)
                controller = self.controller_map[userid]
                return SurveyState(u.message, u.agent_index, scenario.uuid, scenario.get_kb(u.agent_index),
                                   scenario.get_kb(1 - u.agent_index),
                                   scenario.attributes, controller.get_result(u.agent_index))

        except sqlite3.IntegrityError:
            print("WARNING: Rolled back transaction")

    def get_waiting_info(self, userid):
        try:
            with self.conn:
                cursor = self.conn.cursor()
                u = self._get_user_info(cursor, userid, assumed_status=Status.Waiting)
                num_seconds = (self.config["status_params"]["waiting"]["num_seconds"] +
                               u.status_timestamp) - current_timestamp_in_seconds()
                return WaitingState(u.message, num_seconds)

        except sqlite3.IntegrityError:
            print("WARNING: Rolled back transaction")

    def get_schema(self):
        return self.schema

    def get_updated_status(self, userid):
        try:
            with self.conn:
                cursor = self.conn.cursor()
                try:
                    u = self._get_user_info(cursor, userid, assumed_status=None)
                    self.logger.debug("Got updated status {:s} for user {:s}".format(u.status, userid))
                    if u.status == Status.Redirected:
                        self._update_user(cursor, userid, connected_status=1, status=Status.Waiting)
                        return Status.Waiting
                    return u.status
                except (UnexpectedStatusException, ConnectionTimeoutException, StatusTimeoutException) as e:
                    # Handle timeouts by performing the relevant update
                    u = self._get_user_info_unchecked(cursor, userid)

                    if u.status == Status.Waiting:
                        if isinstance(e, ConnectionTimeoutException):
                            self.logger.debug("User {:s} is supposed to be in waiting state, got connection "
                                              "timeout. Updating status to connected.".format(userid))
                            self._update_user(cursor, userid, connected_status=1, status=Status.Waiting)
                            return u.status
                        else:
                            self._stop_waiting_and_transition_to_finished(cursor, userid)
                            return Status.Finished
                    elif u.status == Status.Redirected:
                        self._update_user(cursor, userid, connected_status=1, status=Status.Waiting)
                        return Status.Waiting

                    elif u.status == Status.Chat:
                        if isinstance(e, ConnectionTimeoutException):
                            message = Messages.PartnerConnectionTimeout
                        else:
                            message = Messages.ChatExpired

                        self.logger.debug("User {:s} chat status expired, redirecting to waiting".format(userid))
                        self.end_chat_and_redirect(cursor, userid, message=message)
                        return Status.Waiting

                    elif u.status == Status.Finished:
                        self._update_user(cursor, userid, connected_status=1)
                        return Status.Finished

                    elif u.status == Status.Survey:
                        self._update_user(cursor, userid, connected_status=1)
                        return Status.Survey
                    else:
                        raise Exception("Unknown status: {} for user: {}".format(u.status, userid))

        except sqlite3.IntegrityError:
            print("WARNING: Rolled back transaction")

    def get_user_message(self, userid):
        with self.conn:
            cursor = self.conn.cursor()
            u = self._get_user_info_unchecked(cursor, userid)
            return u.message

    def is_chat_valid(self, userid):
        try:
            with self.conn:
                cursor = self.conn.cursor()
                try:
                    u = self._get_user_info(cursor, userid, assumed_status=Status.Chat)
                except UnexpectedStatusException:
                    return False
                except StatusTimeoutException:
                    u = self._get_user_info_unchecked(cursor, userid)
                    self.timeout_chat_and_finish(cursor, userid,
                                                 message=Messages.ChatExpired + " " + Messages.HITCompletionWarning,
                                                 partner_id=u.partner_id)
                    return False
                except ConnectionTimeoutException:
                    return False

                self._update_user(cursor, userid, connected_status=1)
                if not self.is_user_partner_bot(cursor, userid):
                    try:
                        u2 = self._get_user_info(cursor, u.partner_id, assumed_status=Status.Chat)
                    except UnexpectedStatusException:
                        self.logger.debug("User {:s}: Partner not in chat status, redirecting to "
                                          "waiting".format(userid))
                        self.end_chat_and_redirect(cursor, userid, message=Messages.PartnerLeftRoom)
                        return False
                    except StatusTimeoutException:
                        self.timeout_chat_and_finish(cursor, userid,
                                                     message=Messages.ChatExpired + " " + Messages.HITCompletionWarning,
                                                     partner_id=u.partner_id)
                        # self.end_chat_and_transition_to_waiting(cursor, userid, message=Messages.ChatExpired)
                        return False
                    except ConnectionTimeoutException:
                        self.end_chat_and_redirect(cursor, userid,
                                                   message=Messages.PartnerConnectionTimeout)
                        self.logger.debug("User {:s}: Partner connection timed out, redirecting to "
                                          "waiting".format(userid))
                        return False

                    if self.controller_map[userid] != self.controller_map[u.partner_id]:
                        return False

                if self.check_game_over_and_transition(cursor, userid, u.partner_id):
                    return False

                return True

        except sqlite3.IntegrityError:
            print("WARNING: Rolled back transaction")
            return False

    def is_connected(self, userid):
        try:
            with self.conn:
                cursor = self.conn.cursor()
                try:
                    u = self._get_user_info(cursor, userid)
                    return True if u.connected_status == 1 else False
                except (UnexpectedStatusException, ConnectionTimeoutException, StatusTimeoutException) as e:
                    return False

        except sqlite3.IntegrityError:
            print("WARNING: Rolled back transaction")

    def is_game_over(self, userid):
        """
        Checks whether the game that the user defined by userid is in is complete or not
        :param userid:
        :return:
           finished: boolean to tell whether the game is over or not
           complete: boolean to tell whether the game was completed or not. For some tasks these two values could be the same
        """
        controller = self.controller_map[userid]
        return controller.game_over(), controller.complete()

    def is_user_partner_bot(self, cursor, userid):
        u = self._get_user_info_unchecked(cursor, userid)
        return len(u.partner_type) > 0 and u.partner_type != HumanSystem.name()

    def is_status_unchanged(self, userid, assumed_status):
        try:
            with self.conn:
                cursor = self.conn.cursor()
                try:
                    u = self._get_user_info(cursor, userid, assumed_status=assumed_status)
                    self._update_user(cursor, userid, connected_status=1)
                    if u.status == Status.Waiting:
                        self.attempt_join_chat(userid)
                    return True
                except (UnexpectedStatusException, ConnectionTimeoutException, StatusTimeoutException) as e:
                    return False
        except sqlite3.IntegrityError:
            print("WARNING: Rolled back transaction")

    def receive(self, userid):
        controller = self.controller_map[userid]
        if controller is None:
            # fail silently - this just means that receive is called between the time that the chat has ended and the
            # time that the page is refreshed
            return None
        controller.step(self)
        session = self._get_session(userid)
        return session.poll_inbox()

    def report(self, userid, feedback):
        try:
            with self.conn:
                cursor = self.conn.cursor()
                cursor.execute('''INSERT INTO feedback VALUES (?,?)''', (userid, feedback))
        except sqlite3.IntegrityError:
            print("WARNING: Rolled back transaction")

    def send(self, userid, event):
        session = self._get_session(userid)
        session.enqueue(event)
        controller = self.controller_map[userid]
        if controller is None:
            # fail silently because this just means that the user tries to send something after their partner has left
            # (but before the chat has ended)
            return None
        controller.step(self)
        # self.add_event_to_db(controller.get_chat_id(), event)

    def submit_survey(self, userid, data):
        def _user_finished(userid):
            self._update_user(cursor, userid, status=Status.Finished)

        def _update_scenario_db(chat_id, partner_type):
            cursor.execute('''SELECT scenario_id FROM chat WHERE chat_id=?''', (chat_id,))
            scenario_id = cursor.fetchone()[0]
            # make sure that the # of completed dialogues for the scenario is only updated once if both agents are human
            cursor.execute('''SELECT complete FROM scenario WHERE scenario_id=? AND partner_type=?''',
                           (scenario_id, partner_type))
            complete_set = set(json.loads(cursor.fetchone()[0]))
            complete_set.add(chat_id)
            cursor.execute('''
                UPDATE scenario
                SET complete=?
                WHERE scenario_id=? AND partner_type=?''',
                           (json.dumps(list(complete_set)), scenario_id, partner_type))

        try:
            with self.conn:
                cursor = self.conn.cursor()
                user_info = self._get_user_info_unchecked(cursor, userid)
                _update_scenario_db(user_info.chat_id, user_info.partner_type)
                cursor.execute('INSERT INTO survey VALUES (?,?,?,?,?,?,?,?)',
                               (userid, user_info.chat_id, user_info.partner_type,
                                data['fluent'], data['correct'], data['cooperative'],
                                data['humanlike'], data['comments']))
                _user_finished(userid)
                self.logger.debug("User {:s} submitted survey for chat {:s}".format(userid, user_info.chat_id))
        except sqlite3.IntegrityError:
            print("WARNING: Rolled back transaction")

    def update_chat_reward(self, cursor, chat_id, outcome):
        str_outcome = json.dumps(outcome)
        cursor.execute('''UPDATE chat SET outcome=? WHERE chat_id=?''', (str_outcome, chat_id))

    def visualize_chat(self, userid):
        def _get_chat_id():
            try:
                cursor.execute('SELECT chat_id FROM mturk_task WHERE name=?', (userid,))
                return cursor.fetchone()[0]
            except sqlite3.IntegrityError:
                print("WARNING: Rolled back transaction")
                return None

        def _get_agent_type(partner_idx):
            try:
                cursor.execute('''SELECT agent_types FROM chat WHERE chat_id=?''', (chat_id,))
                agent_types = json.loads(cursor.fetchone()[0])
                agent_types = dict((int(k), v) for (k, v) in agent_types.items())
                return agent_types[partner_idx]
            except sqlite3.OperationalError:
                # this should never happen!!
                print "No agent types stored for chat %s" % chat_id
                return None

        def _get_agent_index():
            try:
                cursor.execute('SELECT agent_ids FROM chat WHERE chat_id=?', (chat_id,))
                agent_ids = json.loads(cursor.fetchone()[0])
                agent_ids = dict((int(k), v) for (k, v) in agent_ids.items())
                return 0 if agent_ids[0] == userid else 1
            except sqlite3.OperationalError:
                # this should never happen!!
                print "No agent IDs stored for chat %s" % chat_id
                return None

        with self.conn:
            cursor = self.conn.cursor()
            chat_id = _get_chat_id()
            agent_index = _get_agent_index()
            partner_type = _get_agent_type(1 - agent_index)
            ex = convert_events_to_json(chat_id, cursor, self.scenario_db)
            _, html = visualize_chat(ex.to_dict(), agent=agent_index, partner_type=partner_type)
            return html


class MutualFriendsBackend(BaseBackend):
    def select(self, userid, idx):
        try:
            with self.conn:
                cursor = self.conn.cursor()
                u = self._get_user_info_unchecked(cursor, userid)
                scenario = self.scenario_db.get(u.scenario_id)
                kb = scenario.get_kb(u.agent_index)
                item = kb.items[idx]
                self.send(userid, Event.SelectionEvent(u.agent_index,
                                                       item,
                                                       str(time.time())))
                return item
        except sqlite3.IntegrityError:
            print("WARNING: Rolled back transaction")
            return None


class NegotiationBackend(BaseBackend):
    def should_reject_chat(self, userid, agent_idx):
        with self.conn:
            controller = self.controller_map[userid]
            cursor = self.conn.cursor()
            chat_id = controller.get_chat_id()
            ex = convert_events_to_json(chat_id, cursor, self.scenario_db).to_dict()
            return reject_transcript(ex, agent_idx)

    def check_game_over_and_transition(self, cursor, userid, partner_id):
        def _get_agent_idx():
            chat_id = controller.get_chat_id()
            try:
                with self.conn:
                    cursor = self.conn.cursor()
                    cursor.execute('''SELECT agent_ids FROM chat WHERE chat_id=?''', (chat_id,))
                    agent_ids = json.loads(cursor.fetchone()[0])
                    agent_ids = dict((int(k), v) for (k, v) in agent_ids.items())
                    return 0 if agent_ids[0] == userid else 1

            except sqlite3.IntegrityError:
                print("WARNING: Rolled back transaction")
                return None

        controller = self.controller_map[userid]
        agent_idx = _get_agent_idx()
        game_over, game_complete = self.is_game_over(userid)

        if game_over:
            if self.should_reject_chat(userid, 1-agent_idx):
                self.logger.debug("Rejecting chat with ID {:s} for PARTNER of user {:s} (partner agent ID {:d}),"
                                  " and redirecting ".format(controller.get_chat_id(), userid, 1-agent_idx))
                self.end_chat_and_redirect(cursor, partner_id,
                                           message=Messages.NegotiationRedirect + " " + Messages.Waiting)
            else:
                if not self.is_user_partner_bot(cursor, userid):
                    partner_msg, _ = self.get_completion_messages(partner_id)
                    self.logger.debug("Accepted chat with ID {:s} for PARTNER of user {:s} (partner agent ID {:d}), "
                                      "and redirecting "
                                      "to survey".format(controller.get_chat_id(), userid, 1-agent_idx))
                    self.end_chat_and_finish(cursor, partner_id, message=partner_msg)

            if self.should_reject_chat(userid, agent_idx):
                self.logger.debug("Rejecting chat with ID {:s} for user {:s} (agent ID {:d}), and "
                                  "redirecting".format(controller.get_chat_id(), userid, agent_idx))
                self.end_chat_and_redirect(cursor, userid,
                                           message=Messages.NegotiationRedirect + " " + Messages.Waiting)
            else:
                msg, _ = self.get_completion_messages(userid)
                self.logger.debug("Accepted chat with ID {:s} for user {:s} (agent ID {:d}), and redirecting to "
                                  "survey".format(controller.get_chat_id(), userid, agent_idx))
                self.end_chat_and_finish(cursor, userid, message=msg)

            return True

        return False

    def get_completion_messages(self, userid):
        """
        Returns two completion messages: one for the current user and one for the user's partner. This function doesn't
        check whether the user's partner is a bot or not. It just decides which user is the winner of the negotiation
        and assigns completion messages accordingly.
        :param userid:
        :return:
        """
        def _get_agent_idx():
            chat_id = controller.get_chat_id()

            try:
                with self.conn:
                    cursor = self.conn.cursor()
                    cursor.execute('''SELECT agent_ids FROM chat WHERE chat_id=?''', (chat_id,))
                    agent_ids = json.loads(cursor.fetchone()[0])
                    agent_ids = dict((int(k), v) for (k, v) in agent_ids.items())
                    print "Agent IDs string:", agent_ids
                    return 0 if agent_ids[0] == userid else 1

            except sqlite3.IntegrityError:
                print("WARNING: Rolled back transaction")
                return None

        _, game_complete = self.is_game_over(userid)
        if game_complete:
            msg = Messages.get_completed_message()
            partner_msg = msg

            # agent_idx = _get_agent_idx()
            # if agent_idx == controller.get_winner():
            #     msg = Messages.NegotiationBetterDeal
            #     partner_msg = Messages.NegotiationWorseDeal
            # elif controller.get_winner() == 1 - agent_idx:
            #     msg = Messages.NegotiationWorseDeal
            #     partner_msg = Messages.NegotiationBetterDeal
        else:
            msg = Messages.get_incomplete_message()
            partner_msg = msg

        return msg, partner_msg

    def make_offer(self, userid, offer):
        try:
            with self.conn:
                cursor = self.conn.cursor()
                u = self._get_user_info_unchecked(cursor, userid)
                self.send(userid, Event.OfferEvent(u.agent_index,
                                                   offer,
                                                   str(time.time())))
        except sqlite3.IntegrityError:
            print("WARNING: Rolled back transaction")
            return None

    def accept_offer(self, userid):
        try:
            with self.conn:
                cursor = self.conn.cursor()
                u = self._get_user_info_unchecked(cursor, userid)
                self.send(userid, Event.AcceptEvent(u.agent_index,
                                                   str(time.time())))
        except sqlite3.IntegrityError:
            print("WARNING: Rolled back transaction")
            return None

    def reject_offer(self, userid):
        try:
            with self.conn:
                cursor = self.conn.cursor()
                u = self._get_user_info_unchecked(cursor, userid)
                self.send(userid, Event.RejectEvent(u.agent_index,
                                                   str(time.time())))
        except sqlite3.IntegrityError:
            print("WARNING: Rolled back transaction")
            return None

    def quit(self, userid):
        try:
            with self.conn:
                cursor = self.conn.cursor()
                u = self._get_user_info_unchecked(cursor, userid)
                self.send(userid, Event.QuitEvent(u.agent_index,
                                                  None,
                                                  str(time.time())))
        except sqlite3.IntegrityError:
            print("WARNING: Rolled back transaction")
            return None

    def submit_survey(self, userid, data):
        def _user_finished(userid):
            self._update_user(cursor, userid, status=Status.Finished)

        def _update_scenario_db(chat_id, partner_type):
            cursor.execute('''SELECT scenario_id FROM chat WHERE chat_id=?''', (chat_id,))
            scenario_id = cursor.fetchone()[0]
            # make sure that the # of completed dialogues for the scenario is only updated once if both agents are human
            cursor.execute('''SELECT complete FROM scenario WHERE scenario_id=? AND partner_type=?''',
                           (scenario_id, partner_type))
            complete_set = set(json.loads(cursor.fetchone()[0]))
            complete_set.add(chat_id)
            cursor.execute('''
                UPDATE scenario
                SET complete=?
                WHERE scenario_id=? AND partner_type=?
                AND (SELECT COUNT(survey.name)
                    FROM survey
                    WHERE survey.chat_id=?) = 0;
            ''', (json.dumps(list(complete_set)), scenario_id, partner_type, chat_id))

        try:
            with self.conn:
                cursor = self.conn.cursor()
                user_info = self._get_user_info_unchecked(cursor, userid)
                _update_scenario_db(user_info.chat_id, user_info.partner_type)
                cursor.execute('INSERT INTO survey VALUES (?,?,?,?,?,?,?,?,?,?)',
                               (userid, user_info.chat_id, user_info.partner_type,
                                data['fluent'], data['honest'], data['persuasive'],
                                data['fair'], data['negotiator'], data['coherent'], data['comments']))
                _user_finished(userid)
        except sqlite3.IntegrityError:
            print("WARNING: Rolled back transaction")
