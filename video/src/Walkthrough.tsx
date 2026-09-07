import {AbsoluteFill, Audio, OffthreadVideo, Sequence, staticFile} from 'remotion';
import timeline from '../../docs/demo/timeline.json';

const FPS = 30;

/**
 * The screen recording, with each narrated line placed at the second its own beat appeared.
 *
 * The recorder wrote those offsets while it was driving the browser, so nothing here is timed by
 * hand: a beat that took longer to load simply carries a later start, and the voice still lands
 * on the thing it describes.
 */
export const Walkthrough: React.FC = () => (
  <AbsoluteFill style={{backgroundColor: '#0b0d11'}}>
    <OffthreadVideo src={staticFile(timeline.video)} />
    {timeline.beats.map((beat) => (
      <Sequence
        key={beat.id}
        name={beat.id}
        from={Math.round(beat.start * FPS)}
        durationInFrames={Math.ceil(beat.seconds * FPS) + FPS}
      >
        <Audio src={staticFile(`audio/${beat.audio}`)} />
      </Sequence>
    ))}
  </AbsoluteFill>
);
