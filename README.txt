PIP  -  offline voice chat for your Raspberry Pi 500 and Raspberry Pi 4
========================================================================

A cheerful little voice assistant that lives on your Pi. Talk through your
headset; Pip listens, thinks and answers, all on the Pi with no internet.

  Ears   : Whisper (base.en)
  Brain  : Ollama with llama3.2:3b (llama3.2:1b included for quicker replies)
  Voice  : Piper (Amy, Joe and Alba voices)

  On a Raspberry Pi 4 (4 GB) Pip sets itself up smaller and quicker:
  Ears: Whisper tiny.en   Brain: llama3.2:1b (qwen2.5:0.5b for fastest)
  Voice: Amy (low) by default, Amy and Joe (medium) to choose from.
  The installer raises swap to 2 GB if needed so the model has room.

Needs 64-bit Raspberry Pi OS. Plug in your USB headset before starting.


SETUP (once, needs internet)
----------------------------
1. Unzip into your home folder:
       unzip Pip.zip -d ~
2. Run the installer in a terminal:
       cd ~/Pip
       ./install.sh
   It installs Ollama, the speech libraries, voices and models (~3.5 GB).
   Takes 10-20 minutes.
3. Done. A Pip icon is on your desktop (and in the menu under Accessories).


USING IT
--------
Double-click Pip. Click "Start talking" and allow the microphone.

  Push to talk : hold SPACE (or the button), talk, let go
  Hands-free   : just talk; Pip answers when you pause
  Esc          : stop Pip mid-sentence
  Typing       : the text box works too

The web address is shown at the top with a Copy button. Flip
"Share on Wi-Fi" to use Pip from a phone or another computer on your
home network (first visit: choose Advanced -> Continue on the warning).

The gear menu lets you pick the headset mic and output, voice, speed,
Hands-free sensitivity and Pip's personality.

Speed tip: on a Pi, llama3.2:3b takes a few seconds to start answering.
For snappier chat, choose llama3.2:1b in the model picker at the top.


GETTING UPDATES
---------------
Pip checks GitHub when it starts (if the Pi is online) and shows an
"Update available" button at the top when there's something new.

Three easy ways to update - pick any:
  1. In Pip: gear menu -> Updates -> "Update now"
  2. Double-click the "Update Pip" icon on the desktop
  3. In a terminal:  ~/Pip/update.sh

Your AI models, voices, settings, chats and config.json changes are kept.
If Pip is running it restarts by itself and the window reloads.
Updating needs internet; Pip itself stays fully offline.


IF SOMETHING ISN'T WORKING
--------------------------
The dots at the top (Brain / Ears / Voice) go green when ready.
  Brain red -> run:  sudo systemctl restart ollama
  Ears/Voice -> run ./install.sh again to re-download the missing parts
No sound? Right-click the speaker icon on the taskbar and pick your headset,
or choose it in Pip's gear menu.
What happened is logged in  ~/Pip/pip.log
