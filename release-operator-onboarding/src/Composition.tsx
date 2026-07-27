import { Composition } from "remotion";
import { TransitionSeries, linearTiming } from "@remotion/transitions";
import { fade } from "@remotion/transitions/fade";
import {
  BothVpnsScene,
  BootstrapScene,
  CommandsScene,
  ConfigScene,
  GuidedScene,
  OutroScene,
  PosturesScene,
  RoleScene,
  StagesScene,
  TitleScene,
  TrainingScene,
  WriteProbeScene,
} from "./scenes/Scenes";
import {
  FPS,
  HEIGHT,
  TRANSITION_FRAMES,
  WIDTH,
  sceneDurations,
  totalDurationInFrames,
} from "./theme";

const transitionTiming = linearTiming({
  durationInFrames: TRANSITION_FRAMES,
});

const OnboardingTutorial: React.FC = () => {
  return (
    <TransitionSeries>
      <TransitionSeries.Sequence
        durationInFrames={sceneDurations.title}
        premountFor={FPS}
      >
        <TitleScene />
      </TransitionSeries.Sequence>
      <TransitionSeries.Transition
        presentation={fade()}
        timing={transitionTiming}
      />
      <TransitionSeries.Sequence
        durationInFrames={sceneDurations.role}
        premountFor={FPS}
      >
        <RoleScene />
      </TransitionSeries.Sequence>
      <TransitionSeries.Transition
        presentation={fade()}
        timing={transitionTiming}
      />
      <TransitionSeries.Sequence
        durationInFrames={sceneDurations.postures}
        premountFor={FPS}
      >
        <PosturesScene />
      </TransitionSeries.Sequence>
      <TransitionSeries.Transition
        presentation={fade()}
        timing={transitionTiming}
      />
      <TransitionSeries.Sequence
        durationInFrames={sceneDurations.bothVpns}
        premountFor={FPS}
      >
        <BothVpnsScene />
      </TransitionSeries.Sequence>
      <TransitionSeries.Transition
        presentation={fade()}
        timing={transitionTiming}
      />
      <TransitionSeries.Sequence
        durationInFrames={sceneDurations.bootstrap}
        premountFor={FPS}
      >
        <BootstrapScene />
      </TransitionSeries.Sequence>
      <TransitionSeries.Transition
        presentation={fade()}
        timing={transitionTiming}
      />
      <TransitionSeries.Sequence
        durationInFrames={sceneDurations.config}
        premountFor={FPS}
      >
        <ConfigScene />
      </TransitionSeries.Sequence>
      <TransitionSeries.Transition
        presentation={fade()}
        timing={transitionTiming}
      />
      <TransitionSeries.Sequence
        durationInFrames={sceneDurations.stages}
        premountFor={FPS}
      >
        <StagesScene />
      </TransitionSeries.Sequence>
      <TransitionSeries.Transition
        presentation={fade()}
        timing={transitionTiming}
      />
      <TransitionSeries.Sequence
        durationInFrames={sceneDurations.training}
        premountFor={FPS}
      >
        <TrainingScene />
      </TransitionSeries.Sequence>
      <TransitionSeries.Transition
        presentation={fade()}
        timing={transitionTiming}
      />
      <TransitionSeries.Sequence
        durationInFrames={sceneDurations.writeProbe}
        premountFor={FPS}
      >
        <WriteProbeScene />
      </TransitionSeries.Sequence>
      <TransitionSeries.Transition
        presentation={fade()}
        timing={transitionTiming}
      />
      <TransitionSeries.Sequence
        durationInFrames={sceneDurations.guided}
        premountFor={FPS}
      >
        <GuidedScene />
      </TransitionSeries.Sequence>
      <TransitionSeries.Transition
        presentation={fade()}
        timing={transitionTiming}
      />
      <TransitionSeries.Sequence
        durationInFrames={sceneDurations.commands}
        premountFor={FPS}
      >
        <CommandsScene />
      </TransitionSeries.Sequence>
      <TransitionSeries.Transition
        presentation={fade()}
        timing={transitionTiming}
      />
      <TransitionSeries.Sequence
        durationInFrames={sceneDurations.outro}
        premountFor={FPS}
      >
        <OutroScene />
      </TransitionSeries.Sequence>
    </TransitionSeries>
  );
};

export const OnboardingComposition: React.FC = () => {
  return (
    <Composition
      id="ReleaseOperatorOnboarding"
      component={OnboardingTutorial}
      durationInFrames={totalDurationInFrames}
      fps={FPS}
      width={WIDTH}
      height={HEIGHT}
    />
  );
};
