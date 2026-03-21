import { createContext, useContext } from 'react';

type SetActiveArtifact = (artifact: any) => void;

const ArtifactContext = createContext<SetActiveArtifact>(() => {});

export const ArtifactProvider = ArtifactContext.Provider;

export function useSetArtifact(): SetActiveArtifact {
  return useContext(ArtifactContext);
}
