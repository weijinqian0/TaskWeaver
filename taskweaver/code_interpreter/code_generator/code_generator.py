import os
from typing import List, Optional

from injector import inject

from taskweaver.code_interpreter.code_generator.plugin_selection import PluginSelector, SelectedPluginPool
from taskweaver.config.module_config import ModuleConfig
from taskweaver.llm import LLMApi
from taskweaver.llm.util import ChatMessageType, format_chat_message
from taskweaver.logging import TelemetryLogger
from taskweaver.memory import Attachment, Conversation, Memory, Post, Round, RoundCompressor
from taskweaver.memory.plugin import PluginEntry, PluginRegistry
from taskweaver.misc.example import load_examples
from taskweaver.role import PostTranslator, Role
from taskweaver.utils import read_yaml


class CodeGeneratorConfig(ModuleConfig):
    def _configure(self) -> None:
        self._set_name("code_generator")
        self.role_name = self._get_str("role_name", "ProgramApe")
        self.executor_name = self._get_str("executor_name", "CodeExecutor")
        self.load_plugin = self._get_bool("load_plugin", True)
        self.load_example = self._get_bool("load_example", True)
        self.prompt_file_path = self._get_path(
            "prompt_file_path",
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "code_generator_json_prompt.yaml",
            ),
        )
        self.example_base_path = self._get_path(
            "example_base_path",
            os.path.join(
                self.src.app_base_path,
                "codeinterpreter_examples",
            ),
        )
        self.prompt_compression = self._get_bool("prompt_compression", False)
        self.compression_prompt_path = self._get_path(
            "compression_prompt_path",
            os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "compression_prompt.yaml",
            ),
        )
        self.enable_auto_plugin_selection = self._get_bool("enable_auto_plugin_selection", False)
        self.auto_plugin_selection_topk = self._get_int("auto_plugin_selection_topk", 3)


class CodeGenerator(Role):
    @inject
    def __init__(
        self,
        config: CodeGeneratorConfig,
        plugin_registry: PluginRegistry,
        logger: TelemetryLogger,
        llm_api: LLMApi,
        round_compressor: RoundCompressor,
    ):
        self.config = config
        self.logger = logger
        self.llm_api = llm_api

        self.role_name = self.config.role_name
        self.executor_name = self.config.executor_name

        self.post_translator = PostTranslator(logger)
        self.prompt_data = read_yaml(self.config.prompt_file_path)

        self.instruction_template = self.prompt_data["content"]

        self.conversation_head_template = self.prompt_data["conversation_head"]
        self.user_message_head_template = self.prompt_data["user_message_head"]
        self.plugin_pool = plugin_registry.get_list()
        self.query_requirements_template = self.prompt_data["requirements"]

        self.examples = None
        self.code_verification_on = None
        self.allowed_modules = None
        self.plugin_only = None

        self.instruction = self.instruction_template.format(
            ROLE_NAME=self.role_name,
            EXECUTOR_NAME=self.executor_name,
        )

        self.round_compressor = round_compressor
        self.compression_template = read_yaml(self.config.compression_prompt_path)["content"]

        if self.config.enable_auto_plugin_selection:
            self.plugin_selector = PluginSelector(plugin_registry, self.llm_api)
            self.plugin_selector.generate_plugin_embeddings()
            logger.info("Plugin embeddings generated")
            self.selected_plugin_pool = SelectedPluginPool()

    def configure_verification(
        self,
        code_verification_on: bool,
        plugin_only: bool,
        allowed_modules: Optional[list] = None,
    ):
        self.plugin_only = plugin_only
        self.allowed_modules = allowed_modules if allowed_modules is not None else []
        self.code_verification_on = code_verification_on

    def compose_verification_requirements(
        self,
        plugin_list: List[PluginEntry],
    ) -> str:
        requirements = []
        if not self.code_verification_on:
            return ""

        if self.plugin_only:
            requirements.append(
                f"- {self.role_name} should only use the following plugins and"
                + " Python built-in functions to complete the task: "
                + ", ".join([f"{plugin.name}" for plugin in plugin_list]),
            )
            requirements.append(f"- {self.role_name} cannot define new functions or plugins.")

        if len(self.allowed_modules) > 0:
            requirements.append(
                f"- {self.role_name} can only import the following Python modules: "
                + ", ".join([f"{module}" for module in self.allowed_modules]),
            )

        if len(self.allowed_modules) == 0 and self.plugin_only:
            requirements.append(f"- {self.role_name} cannot import any Python modules.")
        return "\n".join(requirements)

    def compose_prompt(
        self,
        rounds: List[Round],
        plugins: List[PluginEntry],
    ) -> List[ChatMessageType]:
        chat_history = [format_chat_message(role="system", message=self.instruction)]

        if self.examples is None:
            self.examples = self.load_examples(plugin_only=self.plugin_only)
        for i, example in enumerate(self.examples):
            chat_history.extend(self.compose_conversation(example.rounds, example.plugins))

        summary = None
        if self.config.prompt_compression and self.round_compressor is not None:
            summary, rounds = self.round_compressor.compress_rounds(
                rounds,
                rounds_formatter=lambda _rounds: str(self.compose_conversation(_rounds, plugins)),
                use_back_up_engine=True,
                prompt_template=self.compression_template,
            )

        chat_history.extend(
            self.compose_conversation(
                rounds,
                add_requirements=True,
                summary=summary,
                plugins=plugins,
            ),
        )
        return chat_history

    def compose_conversation(
        self,
        rounds: List[Round],
        plugins: List[PluginEntry],
        add_requirements: bool = False,
        summary: Optional[str] = None,
    ) -> List[ChatMessageType]:
        def format_attachment(attachment: Attachment):
            if attachment.type == "thought":
                return attachment.content.format(ROLE_NAME=self.role_name)
            else:
                return attachment.content

        chat_history = []
        is_first_post = True
        for round_index, conversation_round in enumerate(rounds):
            for post_index, post in enumerate(conversation_round.post_list):
                # compose user query
                user_message = ""
                assistant_message = ""

                if is_first_post:
                    user_message = (
                        self.conversation_head_template.format(
                            SUMMARY="None" if summary is None else summary,
                            PLUGINS="None" if len(plugins) == 0 else self.format_plugins(plugins),
                            ROLE_NAME=self.role_name,
                        )
                        + "\n"
                    )
                    is_first_post = False

                if post.send_from == "Planner" and post.send_to == "CodeInterpreter":
                    user_query = conversation_round.user_query
                    plan = next(iter(post.get_attachment(type="plan")), None)
                    enrichment = ""
                    if plan is not None:
                        enrichment = (
                            f"To complete this request:{user_query}\n\n"
                            f"I have drawn up a plan: \n{plan}\n\n"
                            f"Please proceed with this step of this plan:"
                        )

                    user_message += self.user_message_head_template.format(
                        MESSAGE=f"{enrichment}{post.message}",
                    )
                elif post.send_from == "CodeInterpreter" and post.send_to == "CodeInterpreter":
                    # for code correction
                    user_message += self.user_message_head_template.format(
                        MESSAGE=f"{post.get_attachment('revise_message')[0]}",
                    )

                    assistant_message = self.post_translator.post_to_raw_text(
                        post=post,
                        content_formatter=format_attachment,
                        if_format_message=False,
                        if_format_send_to=False,
                        ignore_types=["revise_message"],
                    )
                elif post.send_from == "CodeInterpreter" and post.send_to == "Planner":
                    assistant_message = self.post_translator.post_to_raw_text(
                        post=post,
                        content_formatter=format_attachment,
                        if_format_message=False,
                        if_format_send_to=False,
                        ignore_types=["revise_message"],
                    )
                else:
                    raise ValueError(f"Invalid post: {post}")

                if len(assistant_message) > 0:
                    chat_history.append(
                        format_chat_message(
                            role="assistant",
                            message=assistant_message,
                        ),
                    )
                if len(user_message) > 0:
                    # add requirements to the last user message
                    if add_requirements and post_index == len(conversation_round.post_list) - 1:
                        user_message += "\n" + self.query_requirements_template.format(
                            PLUGIN_ONLY_PROMPT=self.compose_verification_requirements(plugins),
                            ROLE_NAME=self.role_name,
                        )
                    chat_history.append(
                        format_chat_message(role="user", message=user_message),
                    )

        return chat_history

    def select_plugins_for_prompt(
        self,
        user_query,
    ) -> List[PluginEntry]:
        selected_plugins = self.plugin_selector.plugin_select(
            user_query,
            self.config.auto_plugin_selection_topk,
        )
        self.selected_plugin_pool.add_selected_plugins(selected_plugins)
        self.logger.info(f"Selected plugins: {[p.name for p in selected_plugins]}")
        self.logger.info(f"Selected plugin pool: {[p.name for p in self.selected_plugin_pool.get_plugins()]}")

        return self.selected_plugin_pool.get_plugins()

    def reply(
        self,
        memory: Memory,
        event_handler: callable,
        prompt_log_path: Optional[str] = None,
        use_back_up_engine: Optional[bool] = False,
    ) -> Post:
        # extract all rounds from memory
        rounds = memory.get_role_rounds(
            role="CodeInterpreter",
            include_failure_rounds=False,
        )

        # obtain the user query from the last round
        user_query = rounds[-1].user_query

        if self.config.enable_auto_plugin_selection:
            self.plugin_pool = self.select_plugins_for_prompt(user_query)

        prompt = self.compose_prompt(rounds, self.plugin_pool)

        def early_stop(_type, value):
            if _type in ["text", "python", "sample"]:
                return True
            else:
                return False

        response = self.post_translator.raw_text_to_post(
            llm_output=self.llm_api.chat_completion(prompt, use_backup_engine=use_back_up_engine)["content"],
            send_from="CodeInterpreter",
            event_handler=event_handler,
            early_stop=early_stop,
        )
        response.send_to = "Planner"
        generated_code = ""
        for attachment in response.attachment_list:
            if attachment.type in ["sample", "text"]:
                response.message = attachment.content
                break
            elif attachment.type == "python":
                generated_code = attachment.content
                break

        if self.config.enable_auto_plugin_selection:
            # filter out plugins that are not used in the generated code
            self.selected_plugin_pool.filter_unused_plugins(code=generated_code)

        if prompt_log_path is not None:
            self.logger.dump_log_file(prompt, prompt_log_path)

        return response

    def format_plugins(
        self,
        plugin_list: List[PluginEntry],
    ) -> str:
        if self.config.load_plugin:
            return "\n".join(
                [plugin.format_prompt() for plugin in plugin_list],
            )
        return ""

    def load_examples(
        self,
        plugin_only: bool,
    ) -> List[Conversation]:
        if self.config.load_example:
            return load_examples(folder=self.config.example_base_path, plugin_only=plugin_only)
        return []

    def get_plugin_pool(self) -> List[PluginEntry]:
        return self.plugin_pool


def format_code_revision_message() -> str:
    return (
        "The execution of the previous generated code has failed. "
        "If you think you can fix the problem by rewriting the code, "
        "please generate code and run it again.\n"
        "Otherwise, please explain the problem to me."
    )


def format_output_revision_message() -> str:
    return (
        "Your previous message is not following the output format. "
        "You must generate the output as a JSON object with the following format:\n"
        '{"response": [{"type":"this is the type", "content": "this is the content"}, ...]}\n'
        "You need at least have an element with type 'python' and content being the code to be executed.\n"
        "Don't surround the JSON with ```json and ```, just send the JSON object directly.\n"
        "Please try again."
    )
