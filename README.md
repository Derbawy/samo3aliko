# samo3aliko

## Kitchen Duel (working title)

A 90-second, two-player cooking duel for the browser, built to be shared.

Each round gives both chefs the same kitchen: a pantry of 12 ingredients, one **mystery ingredient** that must be on the plate, a **theme**, and a panel of **three judges**. Players plate ingredients raw or cook them in two pans and a pot, pull them off the heat while the needle is in the green, and serve before the clock runs out. The judges then score both dishes out of 30, with a one-line reaction each.

Open `index.html` in a browser to play. It is a single static file with no build step and no server.

### Modes

| Mode | How it works |
| --- | --- |
| Daily Duel | Everyone gets the same kitchen that day (seeded by date). You play against Robo-Chef. |
| Quick Duel | Random kitchen against Robo-Chef, then challenge a friend. |
| Pass & Play | Two players on one device, cooking one after the other. |
| Duel link / code | After a solo round you get a link (or a code to paste) holding your dish. Your friend cooks in the same kitchen, and your score stays hidden until their reveal. |

### Why it can spread

- **Async challenges with no backend.** The challenge link carries the kitchen seed and your dish, so a friend can play against you without a server.
- **A daily kitchen** gives everyone the same puzzle and something to compare, the way Wordle does.
- **A share card** that shows your dish name, each judge's score and a link back to the game.
- **Judges with personality**, whose one-liners are worth screenshotting. They are Teta Samira (grandma), Chef Marco (Michelin purist), Lina (influencer), Uncle Fuad (chili lover) and Dr. Nour (pharmacist, who flags undercooked food and Canada's priority allergens).

### How scoring works

Every judge weighs the same set of factors differently:
- **Flavour pairings:** combinations that go together score, clashes cost points, and named dishes like Shakshuka or Hummus get a boost.
- **Taste balance:** how many different tastes the dish covers.
- **Doneness:** how well each item was cooked.
- **Portion size.**
- **Colour on the plate.**
- **Theme fit.**
- **Use of the mystery ingredient.**
- **Speed:** time left on the clock.
- **Food safety and health.**

Raw chicken, raw shrimp or raw egg, burnt food, and a missing mystery ingredient all cost heavy penalties. The scoring is deterministic: a dish loaded from a duel code gets the same scores on any device.

### Ideas for next versions

- AI judges, so each dish gets a unique reaction. This needs a small backend to keep the API key private.
- Real-time online duels over WebSockets.
- A mid-round twist at 45 seconds, such as swapping an ingredient or closing a pan.
- A daily leaderboard, streaks and a downloadable share image.
- Arabic language support. The Rubik font already supports Arabic.
- Sound effects: sizzling, a timer beep and a judge reveal sting.
