---
name: commit
description: Create a git commit with emoji prefix
disable-model-invocation: true
argument-hint: [optional message]
allowed-tools: Bash, Glob, Grep, Read
---

Create a git commit following this project's conventions.

## Steps

1. Run `git status` (never use `-uall`) and `git diff --cached` to see staged changes. Also run `git diff` to see unstaged changes.
2. If nothing is staged, ask the user what to stage. Do NOT stage everything with `git add -A` or `git add .` without asking.
3. Analyze the changes and pick the single best emoji from the reference table below.
4. Write a commit message in this format:

```
<emoji> Short summary (imperative, lowercase after emoji)

Optional longer description wrapped at 72 characters. Explain the "why"
not the "what". Always include, unless the change is trivial.
```

5. If the user passed `$ARGUMENTS`, use that as the short summary (still pick the right emoji prefix).
6. Show the user the proposed commit message and ask for confirmation before committing.
7. Commit using a HEREDOC:
```
git commit -m "$(cat <<'EOF'
<message here>
EOF
)"
```

## Style rules

- Atomic commits, split unrelated changes
- Short summary: imperative mood, lowercase after emoji, no period
- Keep the subject line under 72 characters (emoji + space + text)
- Always add long description, unless the change is not trivial
- Never skip git hooks (no `--no-verify`)
- Never amend unless the user explicitly asks
- Do not commit files that look like secrets (.env, credentials, keys)

## Emoji reference


| Emoji | Contexts |
|-------|----------|
| 🐞 | bogus, bug, bugfix |
| 🐛 | bogus, bug, bugfix |
| 🔨 | amend, construct, correct, establish, fix, implement, patch, refactor, repair, rewrite |
| ⚒ | amend, construct, correct, establish, fix, implement, patch, refactor, repair, rewrite |
| 🛠 | amend, construct, correct, establish, fix, implement, patch, refactor, repair, rewrite |
| 🔧 | amend, construct, correct, establish, fix, implement, patch, refactor, repair, rewrite |
| ⛔️ | erroneous, faulty, foul, incorrect, wrong |
| 🚫 | erroneous, faulty, foul, incorrect, wrong |
| 🗃 | archive, packaging, dependencies, library, package |
| 📦 | archive, packaging, dependencies, library, package |
| ➕ | add, append, insert, postfix, prefix, prepend, suffix |
| 🪡 | add, append, insert, postfix, prefix, prepend, suffix |
| 💣 | delete, deleting, deletion, drop, removal, remove, trash |
| 🔥 | delete, deleting, deletion, drop, removal, remove, trash |
| 🗑 | delete, deleting, deletion, drop, removal, remove, trash |
| ✨ | enhance, enhancing, improve, improving, polish |
| 🎨 | beautification, beautify, beauty, cosmetics, embellish, format, prettify, pretty |
| 💄 | beautification, beautify, beauty, cosmetics, embellish, format, prettify, pretty |
| 💅 | beautification, beautify, beauty, cosmetics, embellish, format, prettify, pretty |
| ✅ | confirm, conform, validate, validation, verification, verify |
| ☑️ | confirm, conform, validate, validation, verification, verify |
| ✔️ | confirm, conform, validate, validation, verification, verify |
| 🆕 | feature, featuring, fresh, new |
| 🎁 | feature, featuring, fresh, new |
| 🌱 | feature, featuring, fresh, new |
| 🚀 | deploy, install, launch, publish, set-up, setup |
| 📝 | comment, draft, notation, note, noting, text, register |
| ✍️ | comment, draft, notation, note, noting, text |
| 📚 | book, doc, document, guide, guiding, manual, readme, reference |
| 📘 | book, doc, document, guide, guiding, manual, readme, reference |
| 📙 | book, doc, document, guide, guiding, manual, readme, reference |
| ⏫ | update, updating, upgrade, upgrading |
| ⏬ | degradation, degrade, downgradation, downgrade |
| 🔄 | refresh, re-attempt, reboot, rerun, restart, retry |
| 🔃 | refresh, re-attempt, reboot, rerun, restart, retry |
| 🏷 | revision, version |
| 💡 | awake, daylight, enable, enabling, light, on, wake |
| 🌞 | awake, daylight, enable, enabling, light, on, wake |
| 🌙 | dark, disable, disabling, night, off, sleep |
| 💤 | dark, disable, disabling, night, off, sleep |
| 👕 | lint, style, styling |
| 🚿 | clean |
| ♻️ | recycle, recycling |
| 🕵 | assess, rethink, retro, retrospect, inspect, investigate |
| 🔍 | browse, check, exam, find, lookup, query, review, search, supervise |
| 🔎 | browse, check, exam, find, lookup, query, review, search, supervise |
| 🔬 | browse, check, exam, find, lookup, query, review, search, supervise |
| ⚗️ | test |
| 📏 | align, measure, measuring, meter, transform |
| 📐 | align, measure, measuring, meter, transform |
| ✈️ | move, moving, send, sent, transport |
| 🚁 | move, moving, send, sent, transport |
| 🛩 | move, moving, send, sent, transport |
| 📻 | broadcast, produce, producing, production |
| 📡 | broadcast, produce, producing, production |
| 📺 | broadcast, produce, producing, production, display, show, visible |
| ✉️ | email, mail |
| ❓ | ask, inquiry, query, question, request |
| ❔ | ask, inquiry, query, question, request |
| 📥 | fetch, inbound, receive, take, cover, hold, wrap |
| 👂 | callback, consume, listen, subscribe |
| ⏳ | await, standby, block, pend, wip, work in progress |
| ⏸ | await, standby, hold, interrupt, pause, suspend, wait |
| ⏰ | cron, job, time, timing |
| ⏱ | cron, job, time, timing |
| ⏲ | cron, job, time, timing |
| 📆 | calendar, date, period, repeat, schedule |
| 🗓 | calendar, date, period, repeat, schedule |
| ↩️ | reset, revert, rollback, undo |
| ↪️ | redo |
| 📜 | array, list, queue, stack, log, record |
| ❗️ | beware, caution, notice, warn, warning |
| ❕ | beware, caution, notice, warn, warning |
| ⚠️ | beware, caution, notice, warn, warning |
| ✋ | avoid, prevent, deprecate, bound, ceiling, constrain, limit, restrict, threshold |
| 🛑 | abort, crash, deadlock, error, exception, kill, stop, deprecate |
| 💀 | abort, crash, deadlock, error, exception, kill, stop |
| ☣️ | danger, hazard, breach, compromise, hack, vulnerability |
| ☠️ | danger, hazard, breach, compromise, hack, vulnerability |
| 👨 | human, operator, user |
| 👩 | human, operator, user |
| 🖼 | canvas, css, frontend, html, image, picture, ui, ux |
| ⚙️ | config, option, parameter, setting, setup |
| 🤡 | emulate, fake, impersonate, mock, simulate, stub |
| 🎭 | combine, merge, mix, alternate, switch, emulate, mock, simulate |
| 🔑 | field, key, property, access |
| 🗝 | field, key, property |
| 🏃 | action, execute, play, run, start |
| 🤸‍♂ | behavior, function, method |
| 🤸‍♀ | behavior, function, method |
| 🗒 | body, content, detail |
| 💻 | data |
| 💾 | buffer, memory, persist, save, serialize, storage, store, write |
| 📤 | deserialize, load, read, exclude, exclusion |
| 🔐 | acl, encrypt, guard, hash, hide, lock, protect, secret, secure, security, shield, sign |
| 🛡 | acl, encrypt, guard, hash, hide, lock, protect, secret, secure, security, shield, sign |
| 👮 | authenticate, authentication, authorization, authorize |
| 👮‍♀ | authenticate, authentication, authorization, authorize |
| 📂 | open |
| 🔓 | decode, decrypt, release, reveal, uncover, unleash |
| 🌍 | address, identifier, location, path, route, routing, uri, url |
| 🗺 | address, identifier, location, path, route, routing, uri, url, area, range |
| 📽 | display, show, visible |
| 🖨 | print |
| ✂️ | ignore, jump, skip, exclude, exclusion |
| 🗑 | forget, neglect, overlook |
| ⏯ | attempt, begin, boot, init, initialize, launch, run, start, trial, try |
| ⏹ | abort, end, kill, stop |
| 🎧 | silence, silent, suppress |
| 🚩 | direct, guide, instruct, landmark, navigate, redirect |
| 🏠 | base, home, origin, root |
| 🧠 | brain, center, core |
| ❤️ | heart, middle |
| 🧩 | addin, component, module, plugin |
| 💿 | disc, image |
| 🤹‍♂ | intermediate, middleware, middleman |
| 🤹‍♀ | intermediate, middleware, middleman |
| 🕸 | graph, net, network, radial |
| 🌳 | tree |
| 🍃 | edge, leaf, leaves |
| 🏗 | construct, structural, structure, structuring |
| ☔️ | cover, shadow, support |
| 🚧 | block, hinder, pend, wip, work in progress |
| 👥 | combine, merge, mix |
| 🥂 | handshake, introduce, unify, unite |
| 🤝 | handshake, introduce, unify, unite |
| 👓 | readability, readable |
| ✖️ | cancel, close, disable |
| ❎ | cancel, close, disable |
| ❌ | cancel, close, disable |
| 🗄 | archive, archiving, seal |
| 👐 | cover, hold, safe, shell, wrap |
| 🗳 | cover, hold, safe, shell, wrap |
| 📎 | attach |
| 🖇 | attach |
| ⬆️ | bump, increase, increment, up |
| ⬇️ | decrease, decrement, down |
| ➖ | decrease, decrement, down |
| ↔️ | alternate, alternation, alternative, switch |
| 👶 | immature, initial, premature |
| 🐤 | immature, initial, premature |
| 🔁 | enumerate, iterate, loop, repeat, while |
