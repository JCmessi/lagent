import json
import logging
import queue
import re
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import asdict
from typing import Dict, List, Optional

from termcolor import colored

from lagent.actions import ActionExecutor
from lagent.agents import BaseAgent, Internlm2Agent
from lagent.agents.internlm2_agent import Internlm2Protocol
from lagent.schema import AgentReturn, AgentStatusCode, ModelStatusCode

# 初始化日志记录
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class SearcherAgent(Internlm2Agent):

    def __init__(self, template='{query}', **kwargs) -> None:
        super().__init__(**kwargs)
        self.template = template

    def stream_chat(self,
                    question: str,
                    root_question: str = None,
                    parent_response: List[str] = None,
                    **kwargs) -> AgentReturn:
        message = self.template.format(question=question, topic=root_question)
        print(colored(f'current query: {message}', 'green'))
        for agent_return in super().stream_chat(message, **kwargs):
            agent_return.type = 'searcher'
            agent_return.content = question
            yield deepcopy(agent_return)


class MindSearchProtocol(Internlm2Protocol):

    def __init__(
        self,
        meta_prompt: str = None,
        interpreter_prompt: str = None,
        plugin_prompt: str = None,
        few_shot: Optional[List] = None,
        response_prompt: str = None,
        language: Dict = dict(
            begin='',
            end='',
            belong='assistant',
        ),
        tool: Dict = dict(
            begin='{start_token}{name}\n',
            start_token='<|action_start|>',
            name_map=dict(plugin='<|plugin|>', interpreter='<|interpreter|>'),
            belong='assistant',
            end='<|action_end|>\n',
        ),
        execute: Dict = dict(
            role='execute', begin='', end='', fallback_role='environment'),
    ) -> None:
        self.meta_prompt = meta_prompt
        self.roles_cfg = dict(tool=tool, language=language)
        self.language = language
        self.execute = execute
        self.tool = tool
        self.few_shot = few_shot
        self.interpreter_prompt = interpreter_prompt
        self.plugin_prompt = plugin_prompt
        self.response_prompt = response_prompt

    def format(self,
               inner_step: List[Dict],
               plugin_executor: ActionExecutor = None,
               **kwargs) -> list:
        formatted = []
        if self.meta_prompt:
            formatted.append(dict(role='system', content=self.meta_prompt))
        if self.plugin_prompt:
            plugin_prompt = self.plugin_prompt.format(
                tool_info=json.dumps(
                    plugin_executor.get_actions_info(), ensure_ascii=False))
            formatted.append(
                dict(role='system', content=plugin_prompt, name='plugin'))
        if self.interpreter_prompt:
            formatted.append(
                dict(
                    role='system',
                    content=self.interpreter_prompt,
                    name='interpreter'))
        if self.few_shot:
            for few_shot in self.few_shot:
                formatted += self.format_sub_role(few_shot)
        formatted += self.format_sub_role(inner_step)
        return formatted


class WebSearchGraph:
    end_signal = 'end'
    searcher_resp_queue = queue.Queue()
    searcher_cfg = dict()

    def __init__(self):
        self.nodes = {}
        self.adjacency_list = defaultdict(list)
        self.executor = ThreadPoolExecutor(max_workers=10)
        self.future_to_query = dict()

    def add_root_node(self, node_content, node_name='root'):
        self.nodes[node_name] = dict(content=node_content, type='root')
        self.adjacency_list[node_name] = []
        self.searcher_resp_queue.put((node_name, self.nodes[node_name], []))

    def add_node(self, node_name, node_content):
        self.nodes[node_name] = dict(content=node_content, type='searcher')
        self.adjacency_list[node_name] = []

        def model_stream_thread():
            agent = SearcherAgent(**self.searcher_cfg)
            try:
                for answer in agent.stream_chat(node_content,
                                                self.nodes['root']['content']):
                    self.searcher_resp_queue.put(
                        deepcopy(
                            (node_name,
                             dict(response=answer.response,
                                  detail=answer), [])))
                self.nodes[node_name]['response'] = answer.response
                self.nodes[node_name]['detail'] = answer
            except Exception as e:
                logger.error(f'Error in model_stream_thread: {e}')

        self.future_to_query[self.executor.submit(
            model_stream_thread)] = f'{node_name}-{node_content}'

    def add_response_node(self, node_name='response'):
        self.nodes[node_name] = dict(type='end')
        self.searcher_resp_queue.put((node_name, self.nodes[node_name], []))

    def add_edge(self, start_node, end_node):
        self.adjacency_list[start_node].append(end_node)
        self.searcher_resp_queue.put((start_node, self.nodes[start_node],
                                      self.adjacency_list[start_node]))

    def reset(self):
        self.nodes = {}
        self.adjacency_list = defaultdict(list)

    def node(self, node_name):
        return self.nodes[node_name].copy()


class MindSearchAgent(BaseAgent):

    def __init__(self,
                 llm,
                 searcher_cfg,
                 protocol=MindSearchProtocol(),
                 max_turn=3):
        self.local_dict = {}
        self.ptr = 0
        self.llm = llm
        self.max_turn = max_turn
        WebSearchGraph.searcher_cfg = searcher_cfg
        super().__init__(llm=llm, action_executor=None, protocol=protocol)

    def stream_chat(self, message, **kwargs):
        if isinstance(message, str):
            message = [{'role': 'user', 'content': message}]
        elif isinstance(message, dict):
            message = [message]
        self.local_dict.clear()
        self.ptr = 0
        inner_history = message[:]
        agent_return = AgentReturn()
        agent_return.type = 'planner'
        agent_return.nodes = {}
        agent_return.adjacency_list = {}
        agent_return.inner_steps = deepcopy(inner_history)

        for _ in range(self.max_turn):
            prompt = self._protocol.format(inner_step=inner_history)
            for model_state, response, _ in self.llm.stream_chat(
                    prompt, **kwargs):
                if model_state.value < 0:
                    agent_return.state = getattr(AgentStatusCode,
                                                 model_state.name)
                    yield deepcopy(agent_return)
                    return
                _, language, action = self._protocol.parse(response)
                if not language and not action:
                    continue
                code = action['parameters']['command'] if action else ''
                agent_return.state = self._determine_agent_state(
                    model_state, code, agent_return)
                agent_return.response = language if not code else code

                # if agent_return.state == AgentStatusCode.STREAM_ING:
                yield deepcopy(agent_return)

            inner_history.append({'role': 'language', 'content': language})
            print(colored(response, 'blue'))

            if code:
                yield from self._process_code(agent_return,
                                              inner_history, code,
                                              kwargs.get('as_dict', False))
            else:
                agent_return.state = AgentStatusCode.END
                yield deepcopy(agent_return)
                return

        agent_return.state = AgentStatusCode.END
        yield deepcopy(agent_return)

    def _determine_agent_state(self, model_state, code, agent_return):
        if code:
            return AgentStatusCode.PLUGIN_START if model_state == ModelStatusCode.END else AgentStatusCode.PLUGIN_START
        return AgentStatusCode.ANSWER_ING if agent_return.nodes and 'response' in agent_return.nodes else AgentStatusCode.STREAM_ING

    def _process_code(self, agent_return, inner_history, code, as_dict=False):
        for node_name, node, adj in self.execute_code(code):
            if as_dict and 'detail' in node:
                node['detail'] = asdict(node['detail'])
            agent_return.nodes[node_name] = node
            agent_return.adjacency_list[node_name] = adj
            if not adj:
                yield deepcopy((agent_return, node_name))

        reference, references_url = self._generate_reference(
            agent_return, code)
        inner_history.append({
            'role': 'tool',
            'content': code,
            'name': 'plugin'
        })
        inner_history.append({
            'role': 'environment',
            'content': reference,
            'name': 'plugin'
        })
        agent_return.inner_steps = deepcopy(inner_history)
        agent_return.state = AgentStatusCode.PLUGIN_RETURN
        agent_return.references.update(references_url)
        yield deepcopy(agent_return)

    def _generate_reference(self, agent_return, code):
        node_list = [
            node.strip().strip('\"').strip('\'')
            for node in re.findall(r'graph.node\((.*?)\)', code)
        ]
        if 'add_response_node' in code:
            return self._protocol.response_prompt
        references = []
        references_url = dict()
        for node_name in node_list:
            ref_results = agent_return.nodes[node_name]['actions'][0][
                'result'][0]['content']
            ref_results = json.loads(ref_results)
            ref2url = {idx: item['url'] for idx, item in ref_results.items()}
            ref = f"## {node_name}\n\n{agent_return.nodes[node_name]['response']}\n"
            updated_ref = re.sub(
                r'\[\[(\d+)\]\]',
                lambda match: f'[[{int(match.group(1)) + self.ptr}]]', ref)
            numbers = [int(n) for n in re.findall(r'\[\[(\d+)\]\]', ref)]
            assert all(str(elem) in ref2url for elem in numbers)
            if numbers:
                references_url.update({
                    str(int(idx) + self.ptr): url
                    for idx, url in ref2url.items()
                })
                self.ptr += max(numbers) + 1
            references.append(updated_ref)
        return '\n'.join(references), references_url

    def execute_code(self, command: str):

        def extract_code(text: str) -> str:
            text = re.sub(r'from ([\w.]+) import',
                          'from lagent.agents.mindsearch_agent import', text)
            triple_match = re.search(r'```[^\n]*\n(.+?)```', text, re.DOTALL)
            single_match = re.search(r'`([^`]*)`', text, re.DOTALL)
            if triple_match:
                return triple_match.group(1)
            elif single_match:
                return single_match.group(1)
            return text

        def run_command(cmd):
            try:
                exec(cmd, globals(), self.local_dict)
                plan_graph = self.local_dict.get('graph')
                assert plan_graph is not None
                for future in as_completed(plan_graph.future_to_query):
                    future.result()
                plan_graph.future_to_query.clear()
                plan_graph.searcher_resp_queue.put(plan_graph.end_signal)
            except Exception as e:
                logger.error(f'Error executing code: {e}')

        command = extract_code(command)
        producer_thread = threading.Thread(
            target=run_command, args=(command, ))
        producer_thread.start()

        responses = defaultdict(list)
        ordered_nodes = []
        active_node = None

        while True:
            try:
                item = WebSearchGraph.searcher_resp_queue.get(timeout=60)
                if item is WebSearchGraph.end_signal:
                    for node_name in ordered_nodes:
                        # resp = None
                        for resp in responses[node_name]:
                            yield deepcopy(resp)
                        # if resp:
                        #     assert resp[1][
                        #         'detail'].state == AgentStatusCode.END
                    break
                node_name, node, adj = item
                if node_name in ['root', 'response']:
                    yield deepcopy((node_name, node, adj))
                else:
                    if node_name not in ordered_nodes:
                        ordered_nodes.append(node_name)
                    responses[node_name].append((node_name, node, adj))
                    if not active_node and ordered_nodes:
                        active_node = ordered_nodes[0]
                    while active_node and responses[active_node]:
                        item = responses[active_node].pop(0)
                        if 'detail' in item[1] and item[1][
                                'detail'].state == AgentStatusCode.END:
                            ordered_nodes.pop(0)
                            active_node = None
                        yield deepcopy(item)
            except queue.Empty:
                if not producer_thread.is_alive():
                    break
        producer_thread.join()
        return
