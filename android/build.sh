#!/usr/bin/env bash
# JDK 17, Android SDK platforms;android-35 and build-tools;35.0.0.
# Signing is mandatory. Passwords are read by apksigner from the environment.
set -euo pipefail
: "${ANDROID_HOME:=${ANDROID_SDK_ROOT:?set ANDROID_HOME or ANDROID_SDK_ROOT}}"
: "${COUCHCHD_KEYSTORE:?set release keystore path}" "${COUCHCHD_KEY_ALIAS:?set release key alias}"
: "${COUCHCHD_STORE_PASSWORD:?set release store password}" "${COUCHCHD_KEY_PASSWORD:?set release key password}"
if [[ $# != 3 || ! $2 =~ ^[1-9][0-9]*$ ]]; then
  echo 'usage: bash android/build.sh VERSION_NAME POSITIVE_VERSION_CODE OUTPUT.apk' >&2; exit 2
fi
root=$(cd "$(dirname "$0")" && pwd)
platform="$ANDROID_HOME/platforms/android-35/android.jar"
tools="$ANDROID_HOME/build-tools/35.0.0"
work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
mkdir -p "$work/classes" "$work/dex" "$(dirname "$3")"
"$tools/aapt2" compile --dir "$root/res" -o "$work/resources.zip"
"$tools/aapt2" link -I "$platform" --manifest "$root/AndroidManifest.xml" --version-name "$1" --version-code "$2" -o "$work/base.apk" "$work/resources.zip"
javac -encoding UTF-8 --release 8 -classpath "$platform" -d "$work/classes" "$root"/src/*.java
jar cf "$work/classes.jar" -C "$work/classes" .
"$tools/d8" --release --min-api 26 --lib "$platform" --output "$work/dex" "$work/classes.jar"
jar uf "$work/base.apk" -C "$work/dex" classes.dex
"$tools/zipalign" -f 4 "$work/base.apk" "$work/aligned.apk"
"$tools/apksigner" sign --ks "$COUCHCHD_KEYSTORE" --ks-key-alias "$COUCHCHD_KEY_ALIAS" --ks-pass env:COUCHCHD_STORE_PASSWORD --key-pass env:COUCHCHD_KEY_PASSWORD --v4-signing-enabled false --out "$work/receiver.apk" "$work/aligned.apk"
"$tools/apksigner" verify --print-certs "$work/receiver.apk" > "$work/signer"
if grep -Eiq 'certificate DN:.*CN=Android Debug([,[:space:]]|$)' "$work/signer"; then
  echo 'Refusing an APK signed with an Android debug certificate.' >&2; exit 1
fi
cp "$work/receiver.apk" "$3"
