import asyncio
import json
import os
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from logging import getLogger
from threading import Lock
from typing import Dict, List, Optional, Union

import aiohttp
import requests

from .base_api import AsyncBaseAPILLM, BaseAPILLM

warnings.simplefilter('default')

OPENAI_API_BASE = 'https://api.openai.com/v1/chat/completions'


class GPTAPI(BaseAPILLM):
    """Model wrapper around OpenAI's models.

    Args:
        model_type (str): The name of OpenAI's model.
        query_per_second (int): The maximum queries allowed per second
            between two consecutive calls of the API. Defaults to 1.
        retry (int): Number of retires if the API call fails. Defaults to 2.
        key (str or List[str]): OpenAI key(s). In particular, when it
            is set to "ENV", the key will be fetched from the environment
            variable $OPENAI_API_KEY, as how openai defaults to be. If it's a
            list, the keys will be used in round-robin manner. Defaults to
            'ENV'.
        org (str or List[str], optional): OpenAI organization(s). If not
            specified, OpenAI uses the default organization bound to each API
            key. If specified, the orgs will be posted with each request in
            round-robin manner. Defaults to None.
        meta_template (Dict, optional): The model's meta prompt
            template if needed, in case the requirement of injecting or
            wrapping of any meta instructions.
        openai_api_base (str): The base url of OpenAI's API. Defaults to
            'https://api.openai.com/v1/chat/completions'.
        gen_params: Default generation configuration which could be overridden
            on the fly of generation.
    """

    is_api: bool = True

    def __init__(self,
                 model_type: str = 'gpt-3.5-turbo',
                 retry: int = 2,
                 json_mode: bool = False,
                 key: Union[str, List[str]] = 'ENV',
                 org: Optional[Union[str, List[str]]] = None,
                 meta_template: Optional[Dict] = [
                     dict(role='system', api_role='system'),
                     dict(role='user', api_role='user'),
                     dict(role='assistant', api_role='assistant')
                 ],
                 openai_api_base: str = OPENAI_API_BASE,
                 proxies: Optional[Dict] = None,
                 **gen_params):
        if 'top_k' in gen_params:
            warnings.warn('`top_k` parameter is deprecated in OpenAI APIs.',
                          DeprecationWarning)
            gen_params.pop('top_k')
        super().__init__(
            model_type=model_type,
            meta_template=meta_template,
            retry=retry,
            **gen_params)
        self.gen_params.pop('top_k')
        self.logger = getLogger(__name__)

        if isinstance(key, str):
            self.keys = [os.getenv('OPENAI_API_KEY') if key == 'ENV' else key]
        else:
            self.keys = key

        # record invalid keys and skip them when requesting API
        # - keys have insufficient_quota
        self.invalid_keys = set()

        self.key_ctr = 0
        if isinstance(org, str):
            self.orgs = [org]
        else:
            self.orgs = org
        self.org_ctr = 0
        self.url = openai_api_base
        self.model_type = model_type
        self.proxies = proxies
        self.json_mode = json_mode

    def chat(
        self,
        inputs: Union[List[dict], List[List[dict]]],
        **gen_params,
    ) -> Union[str, List[str]]:
        """Generate responses given the contexts.

        Args:
            inputs (Union[List[dict], List[List[dict]]]): a list of messages
                or list of lists of messages
            gen_params: additional generation configuration

        Returns:
            Union[str, List[str]]: generated string(s)
        """
        assert isinstance(inputs, list)
        if 'max_tokens' in gen_params:
            raise NotImplementedError('unsupported parameter: max_tokens')
        gen_params = {**self.gen_params, **gen_params}
        with ThreadPoolExecutor(max_workers=20) as executor:
            tasks = [
                executor.submit(self._chat, messages, **gen_params)
                for messages in (
                    [inputs] if isinstance(inputs[0], dict) else inputs)
            ]
        ret = [task.result() for task in tasks]
        return ret[0] if isinstance(inputs[0], dict) else ret

    def _chat(self, messages: List[dict], **gen_params) -> str:
        """Generate completion from a list of templates.

        Args:
            messages (List[dict]): a list of prompt dictionaries
            gen_params: additional generation configuration

        Returns:
            str: The generated string.
        """
        assert isinstance(messages, list)
        gen_params = gen_params.copy()

        # Hold out 100 tokens due to potential errors in tiktoken calculation
        max_tokens = min(gen_params.pop('max_new_tokens'), 4096)
        if max_tokens <= 0:
            return ''

        max_num_retries = 0
        while max_num_retries < self.retry:
            with Lock():
                if len(self.invalid_keys) == len(self.keys):
                    raise RuntimeError('All keys have insufficient quota.')

                # find the next valid key
                while True:
                    self.key_ctr += 1
                    if self.key_ctr == len(self.keys):
                        self.key_ctr = 0

                    if self.keys[self.key_ctr] not in self.invalid_keys:
                        break

                key = self.keys[self.key_ctr]

            header = {
                'Authorization': f'Bearer {key}',
                'content-type': 'application/json',
            }

            if self.orgs:
                with Lock():
                    self.org_ctr += 1
                    if self.org_ctr == len(self.orgs):
                        self.org_ctr = 0
                header['OpenAI-Organization'] = self.orgs[self.org_ctr]

            try:
                gen_params_new = gen_params.copy()
                data = dict(
                    model=self.model_type,
                    messages=messages,
                    max_tokens=max_tokens,
                    n=1,
                    stop=gen_params_new.pop('stop_words'),
                    frequency_penalty=gen_params_new.pop('repetition_penalty'),
                    **gen_params_new,
                )
                if self.json_mode:
                    data['response_format'] = {'type': 'json_object'}
                raw_response = requests.post(
                    self.url,
                    headers=header,
                    data=json.dumps(data),
                    proxies=self.proxies)
            except requests.ConnectionError:
                print('Got connection error, retrying...')
                continue
            try:
                response = raw_response.json()
            except requests.JSONDecodeError:
                print('JsonDecode error, got', str(raw_response.content))
                continue
            try:
                return response['choices'][0]['message']['content'].strip()
            except KeyError:
                if 'error' in response:
                    if response['error']['code'] == 'rate_limit_exceeded':
                        time.sleep(1)
                        continue
                    elif response['error']['code'] == 'insufficient_quota':
                        self.invalid_keys.add(key)
                        self.logger.warn(f'insufficient_quota key: {key}')
                        continue

                    print('Find error message in response: ',
                          str(response['error']))
            max_num_retries += 1

        raise RuntimeError('Calling OpenAI failed after retrying for '
                           f'{max_num_retries} times. Check the logs for '
                           'details.')

    def tokenize(self, prompt: str) -> list:
        """Tokenize the input prompt.

        Args:
            prompt (str): Input string.

        Returns:
            list: token ids
        """
        import tiktoken
        self.tiktoken = tiktoken
        enc = self.tiktoken.encoding_for_model(self.model_type)
        return enc.encode(prompt)


class AsyncGPTAPI(AsyncBaseAPILLM):
    """Model wrapper around OpenAI's models.

    Args:
        model (str): The name of OpenAI's model.
        retry (int): Number of retires if the API call fails. Defaults to 2.
        key (str or List[str]): OpenAI key(s). In particular, when it
            is set to "ENV", the key will be fetched from the environment
            variable $OPENAI_API_KEY, as how openai defaults to be. If it's a
            list, the keys will be used in round-robin manner. Defaults to
            'ENV'.
        org (str or List[str], optional): OpenAI organization(s). If not
            specified, OpenAI uses the default organization bound to each API
            key. If specified, the orgs will be posted with each request in
            round-robin manner. Defaults to None.
        meta_template (Dict, optional): The model's meta prompt
            template if needed, in case the requirement of injecting or
            wrapping of any meta instructions.
        api_base (str): The base url of OpenAI's API. Defaults to
            'https://api.openai.com/v1/chat/completions'.
        gen_params: Default generation configuration which could be overridden
            on the fly of generation.
    """

    is_api: bool = True

    def __init__(self,
                 model: str = 'gpt-3.5-turbo',
                 retry: int = 2,
                 json_mode: bool = False,
                 key: Union[str, List[str]] = 'ENV',
                 org: Optional[Union[str, List[str]]] = None,
                 meta_template: Optional[Dict] = [
                     dict(role='system', api_role='system'),
                     dict(role='user', api_role='user'),
                     dict(role='assistant', api_role='assistant')
                 ],
                 api_base: str = OPENAI_API_BASE,
                 proxies: Optional[Dict] = None,
                 **gen_params):
        if 'top_k' in gen_params:
            warnings.warn('`top_k` parameter is deprecated in OpenAI APIs.',
                          DeprecationWarning)
            gen_params.pop('top_k')
        super().__init__(
            model_type=model,
            meta_template=meta_template,
            retry=retry,
            **gen_params)
        self.gen_params.pop('top_k')
        self.logger = getLogger(__name__)

        if isinstance(key, str):
            self.keys = [os.getenv('OPENAI_API_KEY') if key == 'ENV' else key]
        else:
            self.keys = key

        # record invalid keys and skip them when requesting API
        # - keys have insufficient_quota
        self.invalid_keys = set()

        self.key_ctr = 0
        if isinstance(org, str):
            self.orgs = [org]
        else:
            self.orgs = org
        self.org_ctr = 0
        self.url = api_base
        self.model_type = model
        self.proxies = proxies
        self.json_mode = json_mode

    async def chat(
        self,
        inputs: Union[List[dict], List[List[dict]]],
        **gen_params,
    ) -> Union[str, List[str]]:
        """Generate responses given the contexts.

        Args:
            inputs (Union[List[dict], List[List[dict]]]): a list of messages
                or list of lists of messages
            gen_params: additional generation configuration

        Returns:
            Union[str, List[str]]: generated string(s)
        """
        assert isinstance(inputs, list)
        if 'max_tokens' in gen_params:
            raise NotImplementedError('unsupported parameter: max_tokens')
        gen_params = {**self.gen_params, **gen_params}
        tasks = [
            self._chat(messages, **gen_params) for messages in (
                [inputs] if isinstance(inputs[0], dict) else inputs)
        ]
        ret = await asyncio.gather(*tasks)
        return ret[0] if isinstance(inputs[0], dict) else ret

    async def _chat(self, messages: List[dict], **gen_params) -> str:
        """Generate completion from a list of templates.

        Args:
            messages (List[dict]): a list of prompt dictionaries
            gen_params: additional generation configuration

        Returns:
            str: The generated string.
        """
        assert isinstance(messages, list)
        gen_params = gen_params.copy()

        # Hold out 100 tokens due to potential errors in tiktoken calculation
        max_tokens = min(gen_params.pop('max_new_tokens'), 4096)
        if max_tokens <= 0:
            return ''

        max_num_retries = 0
        while max_num_retries < self.retry:
            if len(self.invalid_keys) == len(self.keys):
                raise RuntimeError('All keys have insufficient quota.')

            # find the next valid key
            while True:
                self.key_ctr += 1
                if self.key_ctr == len(self.keys):
                    self.key_ctr = 0

                if self.keys[self.key_ctr] not in self.invalid_keys:
                    break

            key = self.keys[self.key_ctr]

            header = {
                'Authorization': f'Bearer {key}',
                'content-type': 'application/json',
            }

            if self.orgs:
                self.org_ctr += 1
                if self.org_ctr == len(self.orgs):
                    self.org_ctr = 0
                header['OpenAI-Organization'] = self.orgs[self.org_ctr]

            try:
                gen_params_new = gen_params.copy()
                data = dict(
                    model=self.model_type,
                    messages=messages,
                    max_tokens=max_tokens,
                    n=1,
                    stop=gen_params_new.pop('stop_words'),
                    frequency_penalty=gen_params_new.pop('repetition_penalty'),
                    **gen_params_new,
                )
                if self.json_mode:
                    data['response_format'] = {'type': 'json_object'}
                async with aiohttp.ClientSession() as session:
                    async with session.post(
                            self.url,
                            headers=header,
                            json=data,
                            proxy=self.proxies.get(
                                'https', self.proxies.get('http'))) as resp:
                        response = await resp.json()
            except aiohttp.ClientConnectionError:
                print('Got connection error, retrying...')
                continue
            except requests.JSONDecodeError:
                print('JsonDecode error, got', str(resp.content))
                continue
            try:
                return response['choices'][0]['message']['content'].strip()
            except KeyError:
                if 'error' in response:
                    if response['error']['code'] == 'rate_limit_exceeded':
                        time.sleep(1)
                        continue
                    elif response['error']['code'] == 'insufficient_quota':
                        self.invalid_keys.add(key)
                        self.logger.warn(f'insufficient_quota key: {key}')
                        continue

                    print('Find error message in response: ',
                          str(response['error']))
            max_num_retries += 1

        raise RuntimeError('Calling OpenAI failed after retrying for '
                           f'{max_num_retries} times. Check the logs for '
                           'details.')

    def tokenize(self, prompt: str) -> list:
        """Tokenize the input prompt.

        Args:
            prompt (str): Input string.

        Returns:
            list: token ids
        """
        import tiktoken
        self.tiktoken = tiktoken
        enc = self.tiktoken.encoding_for_model(self.model_type)
        return enc.encode(prompt)
