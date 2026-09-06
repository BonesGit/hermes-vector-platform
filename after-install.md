# Next: configure Vector

This is a **user-installed platform plugin**. Hermes will not load it until
it is on `plugins.enabled` (the installer asks `Enable 'vector-platform' now?`
— choose yes, or run `hermes plugins enable vector-platform`).

Then create the bot identity and allowlist **your** Vector npub:

```bash
hermes gateway setup
# pick Vector → create or import identity → enter YOUR npub
hermes gateway restart
```

Setup downloads `vector-bridge` on Linux/macOS (or cargo-builds it). Share the
printed **bot** npub with contacts and DM it from the Vector app.

Do not put an `nsec` or mnemonic in `~/.hermes/.env`. Full reference:
https://github.com/BonesGit/hermes-vector-platform#setup
