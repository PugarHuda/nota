import {existsSync, readdirSync} from 'node:fs';
import {homedir} from 'node:os';
import {join} from 'node:path';

import {Config} from '@remotion/cli/config';

// The recording and its narration are build outputs, not sources: they live beside the rest of
// the demo material rather than being copied into the video project.
Config.setPublicDir('../docs/demo');
Config.setVideoImageFormat('jpeg');
Config.setCodec('h264');
Config.setOverwriteOutput(true);

// Remotion's own Chrome download does not resolve on this machine, and the recorder already
// requires a Playwright Chromium, so the render reuses that one instead of fetching a second
// browser. Set REMOTION_BROWSER_EXECUTABLE to override.
const browser = process.env.REMOTION_BROWSER_EXECUTABLE ?? newestPlaywrightChromium();
if (browser) {
  Config.setBrowserExecutable(browser);
}

function newestPlaywrightChromium() {
  const root = process.env.PLAYWRIGHT_BROWSERS_PATH ??
    join(homedir(), 'AppData', 'Local', 'ms-playwright');
  if (!existsSync(root)) return null;
  const builds = readdirSync(root)
    .filter((name) => /^chromium-\d+$/.test(name))
    .sort((a, b) => Number(b.split('-')[1]) - Number(a.split('-')[1]));
  for (const build of builds) {
    for (const dir of ['chrome-win64', 'chrome-win']) {
      const exe = join(root, build, dir, 'chrome.exe');
      if (existsSync(exe)) return exe;
    }
  }
  return null;
}
