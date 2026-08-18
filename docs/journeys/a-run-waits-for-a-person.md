# A run waits for a person

The durable proof is run `desk/beleg-menschen-tor`
([#194 comment 5324162239](https://github.com/FlexOr2/atelier-2/issues/194#issuecomment-5324162239)).
A V3 wait node `freigabe` held that run in `WAITING_INPUT`. The operator's
answer carried it to `COMPLETED` (terminal hash `60d94900…`). No billed model
call. The first answer had extra fields; the published refusal named them
(`body/revision_hash`, `body/answer_base64`).

What the cockpit shows of that state, on `main`:

1. Studio lists the run under **Waiting for you**, move **Answer**.
2. The project **This workshop** groups it the same way.
3. Opening the V3 run shows the live graph and the rail; the waiting node is
   the current one. There is no **Answer needed** card on this page. That card
   — **Integer answer** and **Answer** — exists only on V1/V2 runs.
4. The answer that finished `desk/beleg-menschen-tor` went through the
   published door `POST /runs/{ref}/answers`.

The older-format card is landed and e2e-proven. It is a different format's
surface. This journey does not pretend the V3 page has it.

Illustriert: REQ-UI-01, REQ-QUEUE-13
