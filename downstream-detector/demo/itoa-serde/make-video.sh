#!/bin/bash
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

out=".local/downstream-walkthrough"
mkdir -p "$out"

swift -module-cache-path .local/swift-module-cache demo/itoa-serde/render-slides.swift "$out"

say -v Samantha -r 172 -o "$out/voice-1.aiff" "A dependency can keep the same API and still break every consumer."
say -v Samantha -r 172 -o "$out/voice-2.aiff" "This demo tracks two real Rust projects: itoa and serde_json."
say -v Samantha -r 172 -o "$out/voice-3.aiff" "We change one behavior. Formatting the integer one now returns two."
say -v Samantha -r 172 -o "$out/voice-4.aiff" "Agent Substrate runs the baseline and candidate inside a network-isolated actor."
say -v Samantha -r 172 -o "$out/voice-5.aiff" "The baseline passes. The consumer catches three failed assertions."
say -v Samantha -r 172 -o "$out/voice-6.aiff" "Open the full backtraces, reproduce locally, then review the risk."

inputs=()
for i in 1 2 3 4 5 6; do
    inputs+=( -loop 1 -framerate 30 -t 6.4 -i "$out/scene-0${i}.png" )
done
for i in 1 2 3 4 5 6; do
    inputs+=( -i "$out/voice-${i}.aiff" )
done

filters=""
for i in 0 1 2 3 4 5; do
    filters+="[$i:v]fps=30,format=yuv420p,settb=AVTB,setpts=PTS-STARTPTS[v$i];"
    j=$((i + 6))
    filters+="[$j:a]aresample=48000,apad=pad_dur=6.4,atrim=duration=6.4,asetpts=PTS-STARTPTS[a$i];"
done
filters+="[v0][v1]xfade=transition=fade:duration=0.4:offset=6[v01];"
filters+="[a0][a1]acrossfade=d=0.4:c1=tri:c2=tri[a01];"
filters+="[v01][v2]xfade=transition=fade:duration=0.4:offset=12[v012];"
filters+="[a01][a2]acrossfade=d=0.4:c1=tri:c2=tri[a012];"
filters+="[v012][v3]xfade=transition=fade:duration=0.4:offset=18[v0123];"
filters+="[a012][a3]acrossfade=d=0.4:c1=tri:c2=tri[a0123];"
filters+="[v0123][v4]xfade=transition=fade:duration=0.4:offset=24[v01234];"
filters+="[a0123][a4]acrossfade=d=0.4:c1=tri:c2=tri[a01234];"
filters+="[v01234][v5]xfade=transition=fade:duration=0.4:offset=30[vout];"
filters+="[a01234][a5]acrossfade=d=0.4:c1=tri:c2=tri[aout]"

ffmpeg -hide_banner -loglevel warning -y "${inputs[@]}" \
    -filter_complex "$filters" -map '[vout]' -map '[aout]' \
    -c:v libx264 -preset medium -crf 21 -pix_fmt yuv420p \
    -c:a aac -b:a 128k -movflags +faststart demo/itoa-serde/walkthrough.mp4

ffmpeg -hide_banner -loglevel warning -y -i demo/itoa-serde/walkthrough.mp4 \
    -vf "fps=8,scale=640:-1:flags=lanczos,palettegen=max_colors=96:stats_mode=diff" \
    "$out/palette.png"
ffmpeg -hide_banner -loglevel warning -y -i demo/itoa-serde/walkthrough.mp4 -i "$out/palette.png" \
    -filter_complex "fps=8,scale=640:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer" \
    demo/itoa-serde/walkthrough.gif
ffmpeg -hide_banner -loglevel warning -y -ss 26 -i demo/itoa-serde/walkthrough.mp4 \
    -frames:v 1 demo/itoa-serde/walkthrough-poster.png
