# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the terms described in the LICENSE file in
# the root directory of this source tree.


import copy
import uuid
from datetime import datetime
from typing import AsyncGenerator, List

from llama_models.llama3.api.datatypes import ToolPromptFormat

from termcolor import cprint

from llama_toolchain.agentic_system.api import *  # noqa: F403
from llama_toolchain.inference.api import *  # noqa: F403
from llama_toolchain.memory.api import *  # noqa: F403
from llama_toolchain.safety.api import *  # noqa: F403

from llama_toolchain.tools.base import BaseTool
from llama_toolchain.tools.builtin import SingleMessageBuiltinTool

from .safety import SafetyException, ShieldRunnerMixin


class ChatAgent(ShieldRunnerMixin):
    def __init__(
        self,
        agent_config: AgentConfig,
        inference_api: Inference,
        memory_api: Memory,
        safety_api: Safety,
        builtin_tools: List[SingleMessageBuiltinTool],
        max_infer_iters: int = 10,
    ):
        self.agent_config = agent_config
        self.inference_api = inference_api
        self.memory_api = memory_api
        self.safety_api = safety_api

        self.max_infer_iters = max_infer_iters
        self.tools_dict = {t.get_name(): t for t in builtin_tools}

        self.sessions = {}

        ShieldRunnerMixin.__init__(
            self,
            safety_api,
            input_shields=agent_config.input_shields,
            output_shields=agent_config.output_shields,
        )

    def create_session(self, name: str) -> Session:
        session_id = str(uuid.uuid4())
        session = Session(
            session_id=session_id,
            session_name=name,
            turns=[],
            started_at=datetime.now(),
        )
        self.sessions[session_id] = session
        return session

    async def create_and_execute_turn(
        self, request: AgenticSystemTurnCreateRequest
    ) -> AsyncGenerator:
        assert (
            request.session_id in self.sessions
        ), f"Session {request.session_id} not found"

        session = self.sessions[request.session_id]

        messages = []
        for i, turn in enumerate(session.turns):
            # print(f"turn {i}")
            # print_dialog(turn.input_messages)
            messages.extend(turn.input_messages)
            for step in turn.steps:
                if step.step_type == StepType.inference.value:
                    messages.append(step.model_response)
                elif step.step_type == StepType.tool_execution.value:
                    for response in step.tool_responses:
                        messages.append(
                            ToolResponseMessage(
                                call_id=response.call_id,
                                tool_name=response.tool_name,
                                content=response.content,
                            )
                        )
                elif step.step_type == StepType.shield_call.value:
                    response = step.response
                    if response.is_violation:
                        # TODO: Properly persist the
                        # CompletionMessage itself in the ShieldResponse
                        messages.append(
                            CompletionMessage(
                                content=response.violation_return_message,
                                stop_reason=StopReason.end_of_turn,
                            )
                        )

        messages.extend(request.messages)

        # print("processed dialog ======== ")
        # print_dialog(messages)

        turn_id = str(uuid.uuid4())
        start_time = datetime.now()
        yield AgenticSystemTurnResponseStreamChunk(
            event=AgenticSystemTurnResponseEvent(
                payload=AgenticSystemTurnResponseTurnStartPayload(
                    turn_id=turn_id,
                )
            )
        )

        steps = []
        output_message = None
        async for chunk in self.run(
            session=session,
            turn_id=turn_id,
            input_messages=messages,
            attachments=request.attachments or [],
            sampling_params=self.agent_config.sampling_params,
            stream=request.stream,
        ):
            if isinstance(chunk, CompletionMessage):
                cprint(
                    f"{chunk.role.capitalize()}: {chunk.content}",
                    "white",
                    attrs=["bold"],
                )
                output_message = chunk
                continue

            assert isinstance(
                chunk, AgenticSystemTurnResponseStreamChunk
            ), f"Unexpected type {type(chunk)}"
            event = chunk.event
            if (
                event.payload.event_type
                == AgenticSystemTurnResponseEventType.step_complete.value
            ):
                steps.append(event.payload.step_details)

            yield chunk

        assert output_message is not None

        turn = Turn(
            turn_id=turn_id,
            session_id=request.session_id,
            input_messages=request.messages,
            output_message=output_message,
            started_at=start_time,
            completed_at=datetime.now(),
            steps=steps,
        )
        session.turns.append(turn)

        chunk = AgenticSystemTurnResponseStreamChunk(
            event=AgenticSystemTurnResponseEvent(
                payload=AgenticSystemTurnResponseTurnCompletePayload(
                    turn=turn,
                )
            )
        )
        yield chunk

    async def run(
        self,
        session: Session,
        turn_id: str,
        input_messages: List[Message],
        attachments: List[Attachment],
        sampling_params: SamplingParams,
        stream: bool = False,
    ) -> AsyncGenerator:
        # Doing async generators makes downstream code much simpler and everything amenable to
        # streaming. However, it also makes things complicated here because AsyncGenerators cannot
        # return a "final value" for the `yield from` statement. we simulate that by yielding a
        # final boolean (to see whether an exception happened) and then explicitly testing for it.

        async for res in self.run_shields_wrapper(
            turn_id, input_messages, self.input_shields, "user-input"
        ):
            if isinstance(res, bool):
                return
            else:
                yield res

        async for res in self._run(
            turn_id, session, input_messages, attachments, sampling_params, stream
        ):
            if isinstance(res, bool):
                return
            elif isinstance(res, CompletionMessage):
                final_response = res
                break
            else:
                yield res

        assert final_response is not None
        # for output shields run on the full input and output combination
        messages = input_messages + [final_response]

        async for res in self.run_shields_wrapper(
            turn_id, messages, self.output_shields, "assistant-output"
        ):
            if isinstance(res, bool):
                return
            else:
                yield res

        yield final_response

    async def run_shields_wrapper(
        self,
        turn_id: str,
        messages: List[Message],
        shields: List[ShieldDefinition],
        touchpoint: str,
    ) -> AsyncGenerator:
        if len(shields) == 0:
            return

        step_id = str(uuid.uuid4())
        try:
            yield AgenticSystemTurnResponseStreamChunk(
                event=AgenticSystemTurnResponseEvent(
                    payload=AgenticSystemTurnResponseStepStartPayload(
                        step_type=StepType.shield_call.value,
                        step_id=step_id,
                        metadata=dict(touchpoint=touchpoint),
                    )
                )
            )
            await self.run_shields(messages, shields)

        except SafetyException as e:
            yield AgenticSystemTurnResponseStreamChunk(
                event=AgenticSystemTurnResponseEvent(
                    payload=AgenticSystemTurnResponseStepCompletePayload(
                        step_type=StepType.shield_call.value,
                        step_details=ShieldCallStep(
                            step_id=step_id,
                            turn_id=turn_id,
                            response=e.response,
                        ),
                    )
                )
            )

            yield CompletionMessage(
                content=str(e),
                stop_reason=StopReason.end_of_turn,
            )
            yield False

        yield AgenticSystemTurnResponseStreamChunk(
            event=AgenticSystemTurnResponseEvent(
                payload=AgenticSystemTurnResponseStepCompletePayload(
                    step_type=StepType.shield_call.value,
                    step_details=ShieldCallStep(
                        step_id=step_id,
                        turn_id=turn_id,
                        response=ShieldResponse(
                            # TODO: fix this, give each shield a shield type method and
                            # fire one event for each shield run
                            shield_type=BuiltinShield.llama_guard,
                            is_violation=False,
                        ),
                    ),
                )
            )
        )

    async def _run(
        self,
        session: Session,
        turn_id: str,
        input_messages: List[Message],
        attachments: List[Attachment],
        sampling_params: SamplingParams,
        stream: bool = False,
    ) -> AsyncGenerator:
        enabled_tools = set(t.type for t in self.agent_config.tools)
        need_rag_context = await self._should_retrieve_context(
            input_messages, attachments
        )
        if need_rag_context:
            step_id = str(uuid.uuid4())
            yield AgenticSystemTurnResponseStreamChunk(
                event=AgenticSystemTurnResponseEvent(
                    payload=AgenticSystemTurnResponseStepStartPayload(
                        step_type=StepType.memory_retrieval.value,
                        step_id=step_id,
                    )
                )
            )

            # TODO: find older context from the session and either replace it
            # or append with a sliding window. this is really a very simplistic implementation
            rag_context, bank_ids = await self._retrieve_context(input_messages)

            step_id = str(uuid.uuid4())
            yield AgenticSystemTurnResponseStreamChunk(
                event=AgenticSystemTurnResponseEvent(
                    payload=AgenticSystemTurnResponseStepCompletePayload(
                        step_type=StepType.memory_retrieval.value,
                        step_id=step_id,
                        step_details=MemoryRetrievalStep(
                            memory_bank_ids=bank_ids,
                            inserted_context=rag_context,
                        ),
                    )
                )
            )

            if rag_context:
                system_message = next(m for m in input_messages if m.role == "system")
                if system_message:
                    system_message.content = system_message.content + "\n" + rag_context
                else:
                    input_messages = [
                        Message(role="system", content=rag_context)
                    ] + input_messages

        elif attachments and AgenticSystemTool.code_interpreter.value in enabled_tools:
            urls = [a.content for a in attachments if isinstance(a.content, URL)]
            input_messages.append(attachment_message(urls))

        output_attachments = []

        n_iter = 0
        while True:
            msg = input_messages[-1]
            if msg.role == Role.user.value:
                color = "blue"
            elif msg.role == Role.ipython.value:
                color = "yellow"
            else:
                color = None
            cprint(f"{str(msg)}", color=color)

            step_id = str(uuid.uuid4())
            yield AgenticSystemTurnResponseStreamChunk(
                event=AgenticSystemTurnResponseEvent(
                    payload=AgenticSystemTurnResponseStepStartPayload(
                        step_type=StepType.inference.value,
                        step_id=step_id,
                    )
                )
            )

            req = ChatCompletionRequest(
                model=self.agent_config.model,
                messages=input_messages,
                tools=self.agent_config.tools,
                tool_prompt_format=self.agent_config.tool_prompt_format,
                stream=True,
                sampling_params=sampling_params,
            )

            tool_calls = []
            content = ""
            stop_reason = None
            async for chunk in self.inference_api.chat_completion(req):
                event = chunk.event
                if event.event_type == ChatCompletionResponseEventType.start:
                    continue
                elif event.event_type == ChatCompletionResponseEventType.complete:
                    stop_reason = StopReason.end_of_turn
                    continue

                delta = event.delta
                if isinstance(delta, ToolCallDelta):
                    if delta.parse_status == ToolCallParseStatus.success:
                        tool_calls.append(delta.content)

                    if stream:
                        yield AgenticSystemTurnResponseStreamChunk(
                            event=AgenticSystemTurnResponseEvent(
                                payload=AgenticSystemTurnResponseStepProgressPayload(
                                    step_type=StepType.inference.value,
                                    step_id=step_id,
                                    model_response_text_delta="",
                                    tool_call_delta=delta,
                                )
                            )
                        )

                elif isinstance(delta, str):
                    content += delta
                    if stream and event.stop_reason is None:
                        yield AgenticSystemTurnResponseStreamChunk(
                            event=AgenticSystemTurnResponseEvent(
                                payload=AgenticSystemTurnResponseStepProgressPayload(
                                    step_type=StepType.inference.value,
                                    step_id=step_id,
                                    model_response_text_delta=event.delta,
                                )
                            )
                        )
                else:
                    raise ValueError(f"Unexpected delta type {type(delta)}")

                if event.stop_reason is not None:
                    stop_reason = event.stop_reason

            stop_reason = stop_reason or StopReason.out_of_tokens
            message = CompletionMessage(
                content=content,
                stop_reason=stop_reason,
                tool_calls=tool_calls,
            )

            yield AgenticSystemTurnResponseStreamChunk(
                event=AgenticSystemTurnResponseEvent(
                    payload=AgenticSystemTurnResponseStepCompletePayload(
                        step_type=StepType.inference.value,
                        step_id=step_id,
                        step_details=InferenceStep(
                            # somewhere deep, we are re-assigning message or closing over some
                            # variable which causes message to mutate later on. fix with a
                            # `deepcopy` for now, but this is symptomatic of a deeper issue.
                            step_id=step_id,
                            turn_id=turn_id,
                            model_response=copy.deepcopy(message),
                        ),
                    )
                )
            )

            if n_iter >= self.max_infer_iters:
                cprint("Done with MAX iterations, exiting.")
                yield message
                break

            if stop_reason == StopReason.out_of_tokens:
                cprint("Out of token budget, exiting.")
                yield message
                break

            if len(message.tool_calls) == 0:
                if stop_reason == StopReason.end_of_turn:
                    # TODO: UPDATE RETURN TYPE TO SEND A TUPLE OF (MESSAGE, ATTACHMENTS)
                    if len(output_attachments) > 0:
                        if isinstance(message.content, list):
                            message.content += attachments
                        else:
                            message.content = [message.content] + attachments
                    yield message
                else:
                    cprint(f"Partial message: {str(message)}", color="green")
                    input_messages = input_messages + [message]
            else:
                cprint(f"{str(message)}", color="green")
                try:
                    tool_call = message.tool_calls[0]

                    name = tool_call.tool_name
                    if not isinstance(name, BuiltinTool):
                        yield message
                        return

                    step_id = str(uuid.uuid4())
                    yield AgenticSystemTurnResponseStreamChunk(
                        event=AgenticSystemTurnResponseEvent(
                            payload=AgenticSystemTurnResponseStepStartPayload(
                                step_type=StepType.tool_execution.value,
                                step_id=step_id,
                            )
                        )
                    )
                    yield AgenticSystemTurnResponseStreamChunk(
                        event=AgenticSystemTurnResponseEvent(
                            payload=AgenticSystemTurnResponseStepProgressPayload(
                                step_type=StepType.tool_execution.value,
                                step_id=step_id,
                                tool_call=tool_call,
                            )
                        )
                    )

                    result_messages = await execute_tool_call_maybe(
                        self.tools_dict,
                        [message],
                    )
                    assert (
                        len(result_messages) == 1
                    ), "Currently not supporting multiple messages"
                    result_message = result_messages[0]

                    yield AgenticSystemTurnResponseStreamChunk(
                        event=AgenticSystemTurnResponseEvent(
                            payload=AgenticSystemTurnResponseStepCompletePayload(
                                step_type=StepType.tool_execution.value,
                                step_details=ToolExecutionStep(
                                    step_id=step_id,
                                    turn_id=turn_id,
                                    tool_calls=[tool_call],
                                    tool_responses=[
                                        ToolResponse(
                                            call_id=result_message.call_id,
                                            tool_name=result_message.tool_name,
                                            content=result_message.content,
                                        )
                                    ],
                                ),
                            )
                        )
                    )

                    # TODO: add tool-input touchpoint and a "start" event for this step also
                    # but that needs a lot more refactoring of Tool code potentially
                    yield AgenticSystemTurnResponseStreamChunk(
                        event=AgenticSystemTurnResponseEvent(
                            payload=AgenticSystemTurnResponseStepCompletePayload(
                                step_type=StepType.shield_call.value,
                                step_details=ShieldCallStep(
                                    step_id=str(uuid.uuid4()),
                                    turn_id=turn_id,
                                    response=ShieldResponse(
                                        # TODO: fix this, give each shield a shield type method and
                                        # fire one event for each shield run
                                        shield_type=BuiltinShield.llama_guard,
                                        is_violation=False,
                                    ),
                                ),
                            )
                        )
                    )

                except SafetyException as e:
                    yield AgenticSystemTurnResponseStreamChunk(
                        event=AgenticSystemTurnResponseEvent(
                            payload=AgenticSystemTurnResponseStepCompletePayload(
                                step_type=StepType.shield_call.value,
                                step_details=ShieldCallStep(
                                    step_id=str(uuid.uuid4()),
                                    turn_id=turn_id,
                                    response=e.response,
                                ),
                            )
                        )
                    )

                    yield CompletionMessage(
                        content=str(e),
                        stop_reason=StopReason.end_of_turn,
                    )
                    yield False
                    return

                if isinstance(result_message.content, Attachment):
                    # NOTE: when we push this message back to the model, the model may ignore the
                    # attached file path etc. since the model is trained to only provide a user message
                    # with the summary. We keep all generated attachments and then attach them to final message
                    output_attachments.append(result_message.content)
                elif isinstance(result_message.content, list) or isinstance(
                    result_message.content, tuple
                ):
                    for c in result_message.content:
                        if isinstance(c, Attachment):
                            output_attachments.append(c)

                input_messages = input_messages + [message, result_message]

            n_iter += 1

    async def _ensure_memory_bank(self, session: Session) -> MemoryBank:
        if session.memory_bank is None:
            session.memory_bank = await self.memory_api.create_memory_bank(
                name=f"memory_bank_{session.session_id}",
                config=VectorMemoryBankConfig(
                    embedding_model="sentence-transformer/all-MiniLM-L6-v2",
                ),
            )

        return session.memory_bank

    async def _should_retrieve_context(
        self, messages: List[Message], attachments: List[Attachment]
    ) -> bool:
        enabled_tools = set(t.type for t in self.agent_config.tools)
        if attachments:
            if (
                AgenticSystemTool.code_interpreter.value in enabled_tools
                and self.agent_config.tool_choice == ToolChoice.required
            ):
                return False
        return attachments or AgenticSystemTool.memory.value in enabled_tools

    def _memory_tool_definition(self) -> Optional[MemoryToolDefinition]:
        for t in self.agent_config.tools:
            if t.type == AgenticSystemTool.memory.value:
                return t

        return None

    async def _retrieve_context(
        self, session: Session, messages: List[Message], attachments: List[Attachment]
    ) -> Optional[InterleavedTextMedia]:
        bank_ids = []

        memory = self._memory_tool_definition()
        assert memory is not None, "Memory tool not configured"
        bank_ids.extend(c.bank_id for c in memory.memory_bank_configs)

        if attachments:
            bank = await self._ensure_memory_bank(session)
            bank_ids.append(bank.bank_id)

            documents = [
                MemoryBankDocument(
                    doc_id=str(uuid.uuid4()),
                    content=a.content,
                    mime_type=a.mime_type,
                    metadata={},
                )
                for a in attachments
            ]
            await self.memory_api.insert_documents(bank.bank_id, documents)

        assert len(bank_ids) > 0, "No memory banks configured?"

        query = " ".join(m.content for m in messages)
        tasks = [
            self.memory_api.query_documents(
                bank_id=bank_id,
                query=query,
                params={
                    "max_chunks": 5,
                },
            )
            for bank_id in bank_ids
        ]
        results: List[QueryDocumentsResponse] = await asyncio.gather(*tasks)
        chunks = [c for r in results for c in r.chunks]
        scores = [s for r in results for s in r.scores]

        # sort by score
        chunks, scores = zip(
            *sorted(zip(chunks, scores), key=lambda x: x[1], reverse=True)
        )
        if not chunks:
            return None

        tokens = 0
        picked = []
        for c in chunks[: memory.max_chunks]:
            tokens += c.token_count
            if tokens > memory.max_tokens_in_context:
                cprint(
                    f"Using {len(picked)} chunks; reached max tokens in context: {tokens}",
                    "red",
                )
                break
            picked.append(c)

        return [
            "The following context was retrieved from the memory bank:\n=== START-RETRIEVED-CONTEXT ===\n",
            *picked,
            "\n=== END-RETRIEVED-CONTEXT ===\n",
        ]


def attachment_message(urls: List[URL]) -> ToolResponseMessage:
    content = []

    for url in urls:
        uri = url.uri
        assert uri.startswith("file://")
        filepath = uri[len("file://") :]
        content.append(f'# There is a file accessible to you at "{filepath}"\n')

    return ToolResponseMessage(
        call_id="",
        tool_name=BuiltinTool.code_interpreter,
        content=content,
    )


async def execute_tool_call_maybe(
    tools_dict: Dict[str, BaseTool], messages: List[CompletionMessage]
) -> List[ToolResponseMessage]:
    # While Tools.run interface takes a list of messages,
    # All tools currently only run on a single message
    # When this changes, we can drop this assert
    # Whether to call tools on each message and aggregate
    # or aggregate and call tool once, reamins to be seen.
    assert len(messages) == 1, "Expected single message"
    message = messages[0]

    tool_call = message.tool_calls[0]
    name = tool_call.tool_name
    assert isinstance(name, BuiltinTool)

    name = name.value

    assert name in tools_dict, f"Tool {name} not found"
    tool = tools_dict[name]
    result_messages = await tool.run(messages)
    return result_messages


def print_dialog(messages: List[Message]):
    for i, m in enumerate(messages):
        if m.role == Role.user.value:
            color = "red"
        elif m.role == Role.assistant.value:
            color = "white"
        elif m.role == Role.ipython.value:
            color = "yellow"
        elif m.role == Role.system.value:
            color = "green"
        else:
            color = "white"

        s = str(m)
        cprint(f"{i} ::: {s[:100]}...", color=color)