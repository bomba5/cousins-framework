# Media generation

*Optional: nothing on this page is needed to run a cousin.*

[Cousins](glossary.md#cousin) can generate images, voice clips and short videos through a
service you point them at. It's off until you configure it, and the
framework doesn't ship or pick a provider. This page covers the config,
the three commands, and exactly what goes over the network.

## Off by default

With no `config/media.toml`, `cousin-image`, `cousin-voice` and
`cousin-video` refuse and say which file to write:

```
cousin-image gen "a violet sunrise over a ridge"
#   -> cousin-image: no image provider configured; declare [image] in config/media.toml before generating image
```

Nothing is sent anywhere in that state. There's no free fallback
either: a request goes to the provider you configured, or it fails with
that provider's URL in the error. It never goes somewhere you didn't
choose.

## Configure a provider

Create `config/media.toml` under the framework root (`config/` is
gitignored). One section per kind; configure only the kinds you want:

```toml
[image]
url = "https://media.example.invalid/v1/images/generations"
model = "some-image-model"
key_file = "config/media-image.key"
timeout_s = 120

[voice]
url = "http://192.0.2.10:8000/v1/audio/speech"
model = "some-voice-model"

[video]
url = "https://media.example.invalid/v1/videos/generations"
model = "some-video-model"
key_file = "config/media-video.key"
timeout_s = 600
```

- `url`: required. Without it the kind counts as not configured.
- `model`: passed through to the provider as is.
- `key_file`: optional, relative to the framework root. Its contents
  are sent as `Authorization: Bearer <key>`. Keep it `chmod 600`.
- `timeout_s`: seconds to wait for one generation, default 120. Video
  usually needs more.

There's no example file for this one yet; copy the block above.

### What the provider has to do

The framework POSTs JSON to `url`:

```json
{"model": "some-image-model", "prompt": "a violet sunrise over a ridge"}
```

and saves the response body as the file, as it comes. So the provider
must answer with the image, audio or video bytes themselves. Files are
saved as `.png` for images, `.mp3` for voice and `.mp4` for video,
whatever the provider actually sent.

That's deliberately small, but it means a service that answers with
JSON (a URL to fetch, or base64 inside a JSON object, as the OpenAI
images API does) won't work directly: you'd get a `.png` full of JSON.
Put a small shim in front that makes the call and returns the bytes.
The same goes for anything that needs more than a model and a prompt,
like a voice id or a video length: the commands only send those two
fields, so the shim has to fill in the rest.

A local media gateway, or anything else, can sit behind `url` the
same way. The framework doesn't care what's there as long as
it answers with the file.

## Using it

Each command has two subcommands: `gen` makes a file, `chat` makes a
file and posts it to someone's chat [thread](glossary.md#thread).

```
cousin-image gen "a violet sunrise over a ridge"
#   -> .../cousins/wren/chat/images/wren_1789740000_3fa81c2e.png

cousin-image chat "a violet sunrise" --user ana --caption "morning"
#   -> posted wren_1789740012_9b04d7aa.png to ana

cousin-voice chat "Standup in five minutes." --user ana
cousin-video gen "a slow pan across a misty ridge at dawn"
```

They need `COUSIN_HOME` (the file goes into that cousin's home) and a
framework root. Files land in the cousin home, named
`<slug>_<unix time>_<hash>.<ext>`:

- `chat/images/` for images
- `chat/audio/` for voice
- `chat/video/` for video

Nothing cleans these up; they're the cousin's to manage.

`chat` stores the file as a reply from the cousin to `--user` in the
cousin's own chat history, the same way `cousin-reply` does. The caption
is optional and goes through the outbound filter first
([configuration](configuration.md)), before anything is generated: a
blocked caption leaves no file and no job row. Posting only happens when you ask
for it with `chat`; `gen` never posts anything. The posted file is never
delivered back to the cousin: it made the file, it already knows.

Exit codes: 0 ok, 2 not configured (or bad arguments), 3 caption blocked
by the outbound filter, 4 the provider failed.

Every generation is also a `media` row in the jobs store while the
provider works: done with the file path, or failed with the error. You
can watch them in the console's Jobs view or with `cousin-job list`
([jobs and loops](jobs-and-loops.md)). A refusal because nothing is
configured doesn't make a row.

## In the console

A posted image shows inline in the chat, a voice clip as an audio
player, a video as a looping preview. Click an image or a video to open
the viewer, where the arrow keys walk through every image and video in
the thread. The media button in the chat header hides or shows every
attachment; that choice is stored in your browser only
([chat](chat.md#in-the-console)).

## What reaches the network

With media configured:

- The prompt, and the model name, go to the `url` of that kind, with
  your key if you set one. Whatever that service does with your prompt
  is up to that service. The framework doesn't inspect or redact it, so
  only point it at something you trust with the content.
- Nothing else. There are no calls to any other service, no telemetry,
  and no automatic posting anywhere.
- The generated file stays on your machine. It only leaves if you run
  the Telegram bridge, which uploads a reply's image, video or voice
  file to Telegram ([telegram](telegram.md#interface)).

To turn a kind off again, remove its section (or its `url`) from
`config/media.toml`. The next command refuses.
