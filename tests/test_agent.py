"""The agent loop, driven by a scripted stand-in for the model."""

from __future__ import annotations

from conftest import NOW, all_events, at, nov, seed
from jarvis.core import agent


class ScriptedLLM:
    """Returns prepared assistant messages in order and records what it was sent."""

    def __init__(self, *replies: dict):
        self.replies = list(replies)
        self.calls: list[list[dict]] = []

    def chat(self, messages, tools):
        self.calls.append([dict(m) for m in messages])
        return self.replies.pop(0)


def call(name: str, **arguments) -> dict:
    return {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": arguments}}]}


def say(text: str) -> dict:
    return {"role": "assistant", "content": text}


def test_system_prompt_has_date_and_zone(conn):
    llm = ScriptedLLM(say("Hello."))
    agent.handle("hi", conn, llm, now=NOW)
    system = llm.calls[0][0]["content"]
    assert "Thursday 05 November 2026, 09:00" in system and "Asia/Dubai" in system


def test_change_replies_with_python_confirmation_in_one_model_call(conn):
    llm = ScriptedLLM(call("create_event", title="DBMS quiz", date="monday", start_time="10am", important=True))
    r = agent.handle("DBMS quiz Monday 10am, important", conn, llm, now=NOW)
    assert r.text == "Added DBMS quiz, Monday 9 November at 10 am, marked important."
    assert len(llm.calls) == 1
    assert r.actions[0]["ok"] and r.actions[0]["tool"] == "create_event"
    [ev] = all_events(conn)
    assert ev.raw_text == "DBMS quiz Monday 10am, important"


def test_question_goes_back_to_model_with_tool_result(conn):
    seed(conn, "Dentist appointment", nov(6), (16, 30))
    llm = ScriptedLLM(call("list_events", period="tomorrow"),
                      say("Tomorrow you have a **dentist appointment** at 4:30 pm."))
    r = agent.handle("what's on tomorrow?", conn, llm, now=NOW)
    assert r.text == "Tomorrow you have a dentist appointment at 4:30 pm."
    tool_msg = llm.calls[1][-1]
    assert tool_msg["role"] == "tool" and "Dentist appointment" in tool_msg["content"]


def test_error_lets_model_retry(conn):
    llm = ScriptedLLM(call("create_event", title="Gym", date="blursday"),
                      call("create_event", title="Gym", date="sunday", start_time="6pm"))
    r = agent.handle("gym sunday 6", conn, llm, now=NOW)
    assert "error" in llm.calls[1][-1]["content"]
    assert r.text == "Added Gym, Sunday 8 November at 6 pm."
    assert [e.start for e in all_events(conn)] == [at(nov(8), 18)]


def test_several_calls_in_one_round(conn):
    llm = ScriptedLLM({"role": "assistant", "content": "", "tool_calls": [
        {"function": {"name": "create_event", "arguments": {"title": "Gym", "date": "sunday", "start_time": "6pm"}}},
        {"function": {"name": "create_event", "arguments": {"title": "Call mom", "date": "tonight"}}},
    ]})
    r = agent.handle("gym sunday 6pm and call mom tonight", conn, llm, now=NOW)
    assert r.text == "Added Gym, Sunday 8 November at 6 pm. Added Call mom, today at 8 pm."


def test_thinking_tags_and_markdown_stripped(conn):
    llm = ScriptedLLM(say("<think>hmm</think>\n**Sure** thing."))
    assert agent.handle("hi", conn, llm, now=NOW).text == "Sure thing."


def test_gives_up_after_max_steps(conn):
    llm = ScriptedLLM(*[call("create_event", title="X", date="blursday")] * agent.MAX_STEPS)
    r = agent.handle("x on blursday", conn, llm, now=NOW)
    assert "couldn't finish" in r.text and all_events(conn) == []


def test_food_answer_without_a_tool_is_sent_back(conn):
    llm = ScriptedLLM(say("Dinner on 20 November: Chicken curry, roti."),  # invented
                      call("get_menu", date="20 november", meal="dinner"))
    r = agent.handle("what's for dinner on 20 november?", conn, llm, now=NOW)
    assert r.text == "I don't have that week's menu."
    assert "did not come from a tool" in llm.calls[1][-1]["content"]


def test_food_guess_is_never_passed_on(conn):
    llm = ScriptedLLM(say("Dinner is chicken curry."), say("Dinner is chicken curry, really."))
    r = agent.handle("what's for dinner tonight?", conn, llm, now=NOW)
    assert r.text == agent.NO_GUESSING


def test_food_words_with_a_calendar_action_are_fine(conn):
    llm = ScriptedLLM(call("create_event", title="Lunch with Aisha", date="saturday", start_time="1pm"))
    r = agent.handle("lunch with Aisha saturday 1pm", conn, llm, now=NOW)
    assert r.text == "Added Lunch with Aisha, Saturday 7 November at 1 pm."


def test_memory_carries_a_clarifying_question(conn):
    memory = agent.SessionMemory()
    llm = ScriptedLLM(say("Which football practice, Friday or Saturday?"), say("Okay."))
    agent.handle("cancel football", conn, llm, now=NOW, session="s1", memory=memory)
    agent.handle("saturday", conn, llm, now=NOW, session="s1", memory=memory)
    second = llm.calls[1]
    assert [m["role"] for m in second] == ["system", "user", "assistant", "user"]
    assert second[2]["content"] == "Which football practice, Friday or Saturday?"
    # a different session doesn't see it
    llm2 = ScriptedLLM(say("Hm?"))
    agent.handle("saturday", conn, llm2, now=NOW, session="other", memory=memory)
    assert len(llm2.calls[0]) == 2
