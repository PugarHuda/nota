import {Composition} from 'remotion';
import timeline from '../../docs/demo/timeline.json';
import {Walkthrough} from './Walkthrough';

const FPS = 30;

export const Root: React.FC = () => (
  <Composition
    id="Walkthrough"
    component={Walkthrough}
    // The recording sets the length. Nothing is padded to a round number.
    durationInFrames={Math.ceil(timeline.seconds * FPS)}
    fps={FPS}
    width={timeline.width}
    height={timeline.height}
  />
);
