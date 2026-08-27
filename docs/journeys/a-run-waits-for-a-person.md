# A run waits for a person

The durable proof is run `desk/beleg-menschen-tor`
([#194 comment 5324162239](https://github.com/FlexOr2/atelier-2/issues/194#issuecomment-5324162239)).
A V3 wait node `freigabe` held that run in `WAITING_INPUT`. The operator's
answer carried it to `COMPLETED` (terminal hash `60d94900…`). No billed model
call. The first answer had extra fields; the published refusal named them
(`body/revision_hash`, `body/answer_base64`) — that first field is
`body/workflow_revision_hash` since #322 gave the value one name on every body.

What the cockpit shows of that state, on `main`:

1. Studio lists the run under **Waiting for you**, move **Answer**.
2. The project **This workshop** groups it the same way.
3. Opening the V3 run shows the live graph, the rail, and an **Answer needed**
   card that names the exact question this pause bound. With no inputs that is
   the authored prompt; with inputs it also carries each declared graph input or
   named predecessor output. Restart reads the same composed question from
   durable material. Submitting sends the typed bytes through
   `POST /runs/{ref}/answers`, and a named refusal of that door stays on the card.
   A wait whose published document carries no question is named as such, not as
   a bare node id.
4. V1/V2 runs keep their own **Integer answer** card. The door is the same.

The V3 card is e2e-proven both on a wait-only run and on the committed
`vision-variants` chain whose question includes the visioner's result.

Illustriert: REQ-UI-01, REQ-QUEUE-13
