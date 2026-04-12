# How I Turned Home Assistant Into an AI Household Operator With Nova

Most Home Assistant setups get very good at one thing: automation.

Lights turn on. Doors trigger alerts. Heating follows schedules. Cameras send notifications. Everything works, but the house still feels like a collection of rules.

That was the gap Nova was built to close.

Nova is not just another text-to-speech endpoint bolted onto Home Assistant. It is the reasoning layer that sits on top of Home Assistant and turns raw state changes into household-aware decisions, summaries, and actions. The important part is that it does this without replacing Home Assistant. Home Assistant still owns the truth. Nova becomes the brain that interprets it.

## The Core Idea

The architecture is simple once you stop thinking of AI as the automation engine.

Home Assistant remains responsible for:

- entities
- integrations
- schedules
- helpers
- timers
- hard control logic
- safety-critical behavior

Nova handles the things Home Assistant is not naturally good at:

- contextual interpretation
- summarization
- camera and visitor narration
- escalation tone
- deciding whether something is worth interrupting people for
- speaking like a household assistant instead of a log file

That split matters. It means the house still works even if Nova is offline, but when Nova is online, it feels less like a rule engine and more like a household operator.

## The System Prompt Is the Real Brain

The most powerful part of Nova is not the web avatar, the chat endpoint, or even the proactive services. It is the system prompt.

That prompt is what turns a generic model into a household-specific agent.

In Nova, the system prompt defines:

- operating identity
- decision priorities
- privacy boundaries
- action limits
- the household profile
- the room and device model
- how conservative or proactive Nova should be
- how it should talk to people in the house

Without that prompt, Nova would just be a model with Home Assistant access. With it, Nova understands who lives in the house, which signals are sensitive, which devices should never be touched autonomously, and how to behave like a reliable home operations layer.

That is why I think of the system prompt as Nova’s brain, not as a cosmetic AI setting.

## What the Stack Looks Like

In my setup, Nova runs as a FastAPI backend on port `8001` and integrates with Home Assistant in three ways.

First, Home Assistant can call Nova directly through `rest_command` entries such as:

- `nova_announce`
- `nova_chat`
- `nova_doorbell`
- `nova_motion_driveway`
- `nova_motion_outdoor`

Second, Nova connects back to Home Assistant over websocket and runs proactive services that watch the house independently.

Third, Nova can serve as the conversational layer for text and voice interaction, including Home Assistant state inspection and service control where ACL rules allow it.

So the data flow is not just “HA sends text to AI.” It is a two-way relationship:

- Home Assistant triggers Nova when it knows something happened
- Nova watches Home Assistant when context matters more than a single trigger
- Nova uses the system prompt to reason over the event
- Home Assistant decides where the output should go

## Why Home Assistant Should Still Own the Truth

One of the easiest mistakes in AI-home projects is moving too much deterministic logic into prompts.

I took the opposite route.

The house still relies on Home Assistant for exact truth:

- whether the washing machine cycle is active
- whether the fridge fault sensor is on
- whether quiet hours are active
- whether someone is home
- whether the alarm is armed
- whether a door has been open for five or thirty minutes

That is the right place for those decisions because Home Assistant is stateful, predictable, and restart-safe.

Nova only steps in once that truth already exists.

That gives you a much better failure mode. If Nova fails, the house loses some intelligence and personality, but it does not lose its core automation behavior.

## Where Nova Actually Changes the Experience

The most obvious improvement is in narration.

Doorbells, driveway motion, visitor detection, parcel follow-up, open-door reminders, train briefings, and bedtime summaries all become more useful when they are phrased with context instead of fixed strings.

For example, a normal Home Assistant automation might say:

“Motion detected outside.”

Nova can instead reason about:

- whether it is quiet hours
- whether the event is likely a delivery
- whether it is the driveway or the front door
- whether the house is occupied
- whether this is urgent enough for speech
- whether it should go to phone, speaker, or AWTRIX instead

That is not just better wording. It is better household behavior.

## The House Starts to Feel Cohesive

Once Nova is integrated properly, the automations stop feeling isolated.

The house can do things like:

- give a morning summary of what actually matters
- tell you whether to leave now or wait for a delayed train
- escalate a fridge issue if it persists
- change from speaker output to push notifications during quiet hours
- follow up on a parcel that is still outside when rain is coming
- summarise the home’s unresolved anomalies in one digest instead of five small alerts

This is where the combination of Home Assistant plus Nova becomes stronger than either one on its own.

Home Assistant is excellent at detecting events.
Nova is excellent at deciding which of those events deserve attention.

## The Prompt Matters More Than the Model Choice

A lot of people focus first on which model Nova should use: local Ollama, OpenAI, Gemini, Claude, or a mix.

That matters, but not as much as the system prompt.

If the prompt is weak, the assistant behaves like a generic chatbot with home access.
If the prompt is strong, even a modest model starts behaving like a house-aware operator.

In practice, the prompt is what keeps Nova aligned with:

- safety first
- human overrides first
- privacy boundaries
- household-specific naming
- conservative actions for uncertain situations
- better judgment around what should and should not become an interruption

The model gives Nova language and reasoning capacity.
The prompt tells Nova what kind of mind it is supposed to have.

## Home Assistant Helpers Become AI Control Surfaces

Another lesson from this setup is that Home Assistant helpers become very powerful once Nova is in the loop.

I use helpers like:

- `nova_quiet_hours`
- `nova_do_not_disturb`
- `nova_guest_mode`
- `nova_travel_mode`

Those are not just UI toggles. They are policy switches for the AI layer.

They let the house change Nova’s behavior without editing prompts or code:

- be quiet overnight
- avoid low-priority interruptions
- speak differently when guests are over
- change assumptions when nobody is home

That makes Home Assistant the policy-control plane, while Nova stays the reasoning plane.

## The Best Pattern: Structured Truth, Flexible Language

The integration pattern I trust most now is this:

1. Home Assistant tracks state precisely.
2. Home Assistant exposes summary helpers and sensors where useful.
3. Nova receives structured context.
4. Nova decides how to frame the message.
5. Home Assistant routes the output.

That is a much more reliable pattern than asking an LLM to infer everything from scratch every time.

It also scales better. As the house grows, you do not want every new capability to become a giant prompt. You want Home Assistant to keep producing structured truth and Nova to keep applying household-aware interpretation on top of it.

## Why This Feels Different From Normal Smart Home AI

Most “AI smart home” demos are still command-and-response systems. You ask a question, and something replies.

Nova is more interesting because it works as an operational layer:

- it watches
- it summarizes
- it prioritizes
- it routes
- it escalates
- it adapts tone based on context

And because the system prompt is treated as a first-class control surface, it behaves like *your* household assistant rather than a generic smart speaker personality.

That is the real difference.

Nova is not trying to replace Home Assistant.
It is trying to give Home Assistant judgment.

## Final Thought

If I had to summarize the design in one sentence, it would be this:

Home Assistant runs the house, but Nova helps the house make sense.

That only works if the split is disciplined:

- Home Assistant owns truth
- Nova owns interpretation
- the system prompt defines the household brain

Get that right, and the result is not just a more talkative smart home. It is a home that starts to feel aware of itself.
