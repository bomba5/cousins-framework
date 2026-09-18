# Media generation specification

**Unconfigured, this subsystem generates nothing and reaches no
network.** Media generation is off until a provider is declared in
`config/media.toml`; with no config, `cousin-image`/`cousin-video`/
`cousin-voice` refuse with a message naming the file to write, and no
prompt leaves the machine. A configured provider writes only under the
cousin's own home and posts to chat only on an explicit request -
never automatically, because auto-posting is a write-somewhere the
unconfigured mental model does not expect. This is the largest write-
and-send surface in the framework, so its off state is stated first.

## The provider seam

The framework names no vendor. A media provider is declared in
`config/media.toml` with a per-kind section, a wire contract every
provider fronts, and a credential read from a path or env - the same
shape the embedding service uses, and for the same reason: an HTTP
endpoint in config is configuration, not a dependency.

```toml
[image]
url = "https://example/v1/images/generations"   # your provider
model = "some-model"
key_file = "config/media-image.key"             # gitignored
timeout_s = 120

[voice]
url = "https://example/v1/audio/speech"
model = "some-voice-model"
key_file = "config/media-voice.key"

[video]
url = "https://example/v1/videos/generations"
model = "some-video-model"
key_file = "config/media-video.key"
```

- **One provider per kind, not per vendor.** The three kinds share the
  wire shape (a prompt and parameters in, an asset out) but differ in
  parameters - voice carries a voice id, video a duration - so each
  kind has its own section and its own endpoint, and a single provider
  can serve all three or three different ones can.
- **The wire contract**, documented so any service can be fronted: a
  `POST` of `{"model", "prompt", ...kind params}` returning either
  asset bytes or a URL to fetch. A provider that speaks the
  OpenAI-images/audio shape works unchanged; others adapt behind that
  contract.
- **No vendor, gateway URL, or model catalogue appears in code.** The
  README may name one real service as a non-normative example, exactly
  as it does a local embedding server - a thing to copy, never a
  default.

## No silent cross-vendor fallback

A request goes to the configured provider or it refuses. The source
framework's `auto` mode quietly downgraded to a free public vendor
when no key was present - which sends the prompt to a service the
operator did not choose. That is the perimeter defect this subsystem
exists to avoid: the destination of a prompt is never a surprise. If a
provider is unreachable, the command fails loudly with the provider
named; it does not reroute.

## Storage

Generated assets are written under the cousin's home:
`<home>/chat/{images,audio,video}/<slug>_<timestamp>_<hash>.<ext>`.
There is no retention policy and no garbage collection - the directory
is the cousin's to manage, like the inbound-image inbox. Nothing else
reads it; the file is the handoff.

## Job tracking

Every generation the CLIs run is a `media` row in the jobs store
(`cousin-job`, the console's Jobs view): running while the provider
works, then done with the asset path as its summary, or failed with
the error. An unconfigured kind is refused before a row is made.

## Posting to chat

Posting is explicit: a `gen` subcommand generates and writes a file; a
separate `chat` subcommand generates and posts. Posting reuses the
chat server's attachment path - the `image`/`audio`/`video` columns
the chat-server spec reserved for exactly this subsystem now carry the
asset's path, and the delivery line to the cousin carries a
`[<kind> attached -> Read <path>]` marker, the inbound-image handoff
run in reverse. The bytes never travel in the terminal line; the file
is the handoff there too.

Outbound captions pass the outbound content filter before posting, the
same boundary every outbound surface crosses.

## Stated limits

- **The provider is trusted with the prompt.** Whatever the operator
  configures receives the prompt text and any reference image; the
  framework does not inspect or redact what a configured provider
  does with it. Configure a provider you trust with the content.
- **Reference-image upload is a send.** A kind that accepts a
  reference image uploads it to the provider; that is content leaving
  the box, stated here so it is a choice, not a surprise.

## Consciously excluded

Model catalogues, per-vendor engine menus, quota management, training
locks, smart-speaker announce, and the household's self-hosted gateway
internals belong to the install, not the framework. The framework
ships the seam and the contract; the operator brings the provider.
