from dataclasses import dataclass

from ..config import Settings
from ..domain import normalize_isrc, normalize_title


@dataclass(frozen=True)
class FileInventory:
    available: bool
    isrcs: set[str]
    misc_titles: set[str]


class MusicFilesystem:
    def __init__(self, settings: Settings):
        self.root = settings.music_path
        self.misc_folder_name = settings.misc_folder_name.casefold()

    async def inventory(self) -> FileInventory:
        # The mount is local ZFS and traversal only inspects directory entries.
        # Keep this straightforward; it runs in the background import worker.
        return self._scan()

    async def isrc_filenames(self) -> set[str]:
        return (await self.inventory()).isrcs

    def _scan(self) -> FileInventory:
        found: set[str] = set()
        misc_titles: set[str] = set()
        if self.root is None or not self.root.is_dir():
            return FileInventory(False, found, misc_titles)
        for path in self.root.rglob("*"):
            if not path.is_file():
                continue
            in_misc = self.misc_folder_name in {
                part.casefold() for part in path.relative_to(self.root).parts[:-1]
            }
            if in_misc:
                misc_titles.add(normalize_title(path.stem))
                continue
            isrc = normalize_isrc(path.stem)
            if isrc:
                found.add(isrc)
        return FileInventory(True, found, misc_titles)
