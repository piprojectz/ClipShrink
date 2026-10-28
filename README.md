\# ClipShrink



Pick sections of a video, join them in any order, and shrink the result under a size limit, such as Discord's 10 MB upload cap.



\## Features



\- \*\*Multiple sections\*\*: add as many clips as you want, reorder them with up/down buttons, and include earlier footage after later footage

\- \*\*Range sliders\*\* for each section, or type exact times (`75`, `1:15`, `1:15.5`, `0:01:15`)

\- \*\*Frame preview\*\* of the point you're adjusting

\- \*\*Size targets\*\*: 10 / 50 / 500 MB presets, or enter any number

\- \*\*Smart resolution\*\*: Auto downscales when the bitrate budget is too thin, or force Original / 1080p / 720p / 480p

\- \*\*Two-pass H.264 encoding\*\* with audio bitrate scaled to the budget; automatically retries tighter if the first result overshoots

\- \*\*Drag-and-drop\*\* onto the window (optional), or drop a video onto the script/exe itself



\## Requirements



\- Python 3 (Windows; tkinter is included)

\- \[ffmpeg](https://ffmpeg.org/) and ffprobe on your PATH:

&#x20; ```

&#x20; winget install Gyan.FFmpeg

&#x20; ```

\- Optional, for drag-and-drop:

&#x20; ```

&#x20; pip install tkinterdnd2

&#x20; ```



\## Usage



```

python ClipShrink.py

```



or `python ClipShrink.py path\\to\\video.mp4` to open a file directly.



1\. Drop in or browse for a video

2\. Adjust the first section, then use \*\*+ Add section\*\* for more

3\. Set the max size and resolution

4\. Click \*\*Shrink it\*\*. Output is saved as `<name>\_discord.mp4` next to the source by default



\## Build a standalone .exe



```

pip install pyinstaller

pyinstaller --onefile --noconsole ClipShrink.py

```



The exe lands in `dist\\`. ffmpeg still needs to be on PATH.

