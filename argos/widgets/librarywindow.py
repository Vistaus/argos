import logging
import re
import threading
from enum import Enum, IntEnum
from functools import partial
from pathlib import Path
from typing import List, Optional, Tuple, Union

from gi.repository import Gio, GLib, GObject, Gtk
from gi.repository.GdkPixbuf import Pixbuf

from argos.message import MessageType
from argos.model import AlbumModel, DirectoryModel, Model, PlaylistModel, TrackModel
from argos.utils import elide_maybe
from argos.widgets.albumdetailsbox import AlbumDetailsBox
from argos.widgets.condensedplayingbox import CondensedPlayingBox
from argos.widgets.librarybrowsingprogressbox import LibraryBrowsingProgressBox
from argos.widgets.utils import default_image_pixbuf, scale_album_image

LOGGER = logging.getLogger(__name__)


class DirectoryStoreColumn(IntEnum):
    MARKUP = 0
    TOOLTIP = 1
    URI = 2
    IMAGE_FILE_PATH = 3
    PIXBUF = 4
    FILTER_TEXT = 5
    FILTER_TEXT_SECONDARY = 6
    TYPE = 7


class DirectoryItemType(IntEnum):
    ALBUM = 1
    DIRECTORY = 2
    PLAYLIST = 3
    TRACK = 4


@Gtk.Template(resource_path="/io/github/orontee/Argos/ui/library_window.ui")
class LibraryWindow(Gtk.Box):
    __gtype_name__ = "LibraryWindow"

    library_overlay: Gtk.Overlay = Gtk.Template.Child()
    library_stack: Gtk.Stack = Gtk.Template.Child()
    directory_view: Gtk.IconView = Gtk.Template.Child()

    album_details_box = GObject.Property(type=AlbumDetailsBox)
    filtered_directory_store = GObject.Property(type=Gtk.TreeModelFilter)
    filtering_text = GObject.Property(type=str)

    directory_uri = GObject.Property(type=str)

    def __init__(self, application: Gtk.Application):
        super().__init__()

        self._app = application
        self._model = application.model
        self._settings: Gio.Settings = application.props.settings

        self.props.directory_uri = self._model.library.props.default_uri
        self._parent_uris: List[str] = self._model.library.get_parent_uris(
            self.props.directory_uri
        )

        self.image_size = self._settings.get_int("albums-image-size")
        self._settings.connect(
            "changed::albums-image-size", self._on_image_size_changed
        )
        self._init_default_images()

        self.props.album_details_box = AlbumDetailsBox(application)
        self.library_stack.add_named(self.props.album_details_box, "album_details_page")

        directory_store = Gtk.ListStore(str, str, str, str, Pixbuf, str, str, int)
        self.props.filtered_directory_store = directory_store.filter_new()
        self.props.filtered_directory_store.set_visible_func(self._filter_row, None)
        self.directory_view.set_model(self.props.filtered_directory_store)

        self.directory_view.set_markup_column(DirectoryStoreColumn.MARKUP)
        self.directory_view.set_tooltip_column(DirectoryStoreColumn.TOOLTIP)
        self.directory_view.set_pixbuf_column(DirectoryStoreColumn.PIXBUF)
        self.directory_view.set_item_width(self.image_size)

        self.add(CondensedPlayingBox(application))

        if application.props.disable_tooltips:
            self.directory_view.props.has_tooltip = False

        self._progress_box = LibraryBrowsingProgressBox()
        self._show_progress_box()
        # Show progress box at startup
        self.show_all()

        self.connect(
            "notify::directory-uri", lambda _1, _2: self._update_store(self._model)
        )
        self._model.connect("directory-completed", self._update_store)
        self._model.connect("albums-sorted", self._update_store)
        application.props.download.connect(
            "images-downloaded", self._update_store_pixbufs
        )
        # Don't make expectations on the order both signals are emitted!!

        self._ongoing_store_update = threading.Lock()
        self._abort_pixbufs_update = False

    def _init_default_images(self):
        self._default_images = {
            DirectoryItemType.ALBUM: default_image_pixbuf(
                "media-optical",
                target_width=self.image_size,
            ),
            DirectoryItemType.DIRECTORY: default_image_pixbuf(
                "inode-directory",
                target_width=self.image_size,
            ),
            DirectoryItemType.PLAYLIST: default_image_pixbuf(
                "audio-x-generic",
                target_width=self.image_size,
            ),
            DirectoryItemType.TRACK: default_image_pixbuf(
                "audio-x-generic",
                target_width=self.image_size,
            ),
        }

    def _show_progress_box(self) -> None:
        self.library_overlay.add_overlay(self._progress_box)
        self._progress_box.show_all()
        self.directory_view.hide()

    def _hide_progress_box(self) -> None:
        self.library_overlay.remove(self._progress_box)
        self.directory_view.show()

    def _build_store_item(
        self,
        model: Union[AlbumModel, DirectoryModel, PlaylistModel, TrackModel],
        type: DirectoryItemType,
    ) -> Tuple[str, str, str, str, Pixbuf, str, str, int]:
        artist_name = (
            model.get_property("artist_name")
            if model.find_property("artist_name")
            else None
        )

        image_path = (
            str(model.get_property("image_path"))
            if model.find_property("image_path")
            else ""
        )
        pixbuf = self._default_images[type]

        if artist_name is not None:
            elided_escaped_name = GLib.markup_escape_text(elide_maybe(model.name))
            elided_escaped_artist_name = GLib.markup_escape_text(
                elide_maybe(artist_name)
            )

            escaped_name = GLib.markup_escape_text(model.name)
            escaped_artist_name = GLib.markup_escape_text(artist_name)

            markup_text = f"<b>{elided_escaped_name}</b>\n{elided_escaped_artist_name}"
            tooltip_text = f"<b>{escaped_name}</b>\n{escaped_artist_name}"
        else:
            escaped_name = GLib.markup_escape_text(model.name)
            elided_escaped_name = GLib.markup_escape_text(elide_maybe(model.name))
            markup_text = f"<b>{elided_escaped_name}</b>"
            tooltip_text = f"{escaped_name}"

        return (
            markup_text,
            tooltip_text,
            model.uri,
            image_path,
            pixbuf,
            artist_name or "",
            model.name,
            type.value,
        )

    def set_filtering_text(self, text: str) -> None:
        stripped = text.strip()
        if stripped != self.props.filtering_text:
            LOGGER.debug(f"Filtering library according to {stripped!r}")

            self.props.filtering_text = stripped
            self.props.filtered_directory_store.refilter()

    def _filter_row(
        self,
        model: Gtk.ListStore,
        iter: Gtk.TreeIter,
        data: None,
    ) -> bool:
        if not self.props.filtering_text:
            return True

        pattern = re.escape(self.props.filtering_text)
        text = model.get_value(iter, DirectoryStoreColumn.FILTER_TEXT)
        if re.search(pattern, text, re.IGNORECASE) is not None:
            return True

        secondary_text = model.get_value(
            iter, DirectoryStoreColumn.FILTER_TEXT_SECONDARY
        )
        return re.search(pattern, secondary_text, re.IGNORECASE) is not None

    def _update_store(self, _1: Model, uri: Optional[str] = None) -> None:
        if uri is not None and uri != self.props.directory_uri:
            return

        directory = self._model.get_directory(self.props.directory_uri)
        if directory is None:
            LOGGER.warning(f"Library browser redirected to root directory")
            self.show_directory("", history=False)
            return

        LOGGER.debug(f"Updating directory store for directory {directory.name!r}")

        if self._ongoing_store_update.locked():
            self._abort_pixbufs_update = True
            LOGGER.info("Pixbufs update thread has been requested to abort...")

        image_uris: List[Path] = []
        with self._ongoing_store_update:
            self._abort_pixbufs_update = False
            store = self.props.filtered_directory_store.get_model()
            store.clear()

            for source, item_type in [
                (directory.albums, DirectoryItemType.ALBUM),
                (directory.directories, DirectoryItemType.DIRECTORY),
                (directory.playlists, DirectoryItemType.PLAYLIST),
                (directory.tracks, DirectoryItemType.TRACK),
            ]:
                for model in source:
                    store.append(self._build_store_item(model, item_type))

                    if model.find_property("image_uri"):
                        image_uris.append(model.get_property("image_uri"))

        if len(image_uris) > 0:
            LOGGER.debug("Will fetch images since directory store was just updated")
            self._app.send_message(
                MessageType.FETCH_ALBUM_IMAGES, data={"image_uris": image_uris}
            )

        self._hide_progress_box()

    def _update_store_pixbufs(
        self, _1: Optional[GObject.GObject] = None, *, force: bool = False
    ) -> None:
        thread = threading.Thread(
            target=self._start_store_pixbufs_update_task,
            name="ImagesThread",
            kwargs={"force": force},
            daemon=True,
        )
        thread.start()

    def _start_store_pixbufs_update_task(self, *, force: bool = False) -> None:
        with self._ongoing_store_update:
            # Will wait for ongoing store update to finish

            image_size = self.image_size

            LOGGER.debug(f"Updating library store pixbufs with size {image_size}...")

            store = self.props.filtered_directory_store.get_model()

            def update_pixbuf_at(path: Gtk.TreePath, pixbuf: Pixbuf) -> bool:
                try:
                    store_iter = store.get_iter(path)
                    store.set_value(store_iter, DirectoryStoreColumn.PIXBUF, pixbuf)
                except Exception as e:
                    LOGGER.warning("Failed to set pixbuf", exc_info=e)
                return False

            store_iter = store.get_iter_first()
            while store_iter is not None:
                if self._abort_pixbufs_update:
                    LOGGER.debug("Aborting update of images")
                    break

                image_path, current_pixbuf, raw_library_item_type = store.get(
                    store_iter,
                    DirectoryStoreColumn.IMAGE_FILE_PATH,
                    DirectoryStoreColumn.PIXBUF,
                    DirectoryStoreColumn.TYPE,
                )
                library_item_type = DirectoryItemType(raw_library_item_type)
                default_image = self._default_images[library_item_type]
                path = store.get_path(store_iter)
                if library_item_type in (
                    DirectoryItemType.ALBUM,
                    DirectoryItemType.TRACK,
                ):
                    if image_path:
                        if force or current_pixbuf == default_image:
                            scaled_pixbuf = scale_album_image(
                                image_path,
                                target_width=image_size,
                            )
                            GLib.idle_add(update_pixbuf_at, path, scaled_pixbuf)
                    else:
                        uri = store.get_value(store_iter, DirectoryStoreColumn.URI)
                        LOGGER.debug(f"No image path for {uri}")
                        GLib.idle_add(update_pixbuf_at, path, default_image)
                else:
                    GLib.idle_add(update_pixbuf_at, path, default_image)

                store_iter = store.iter_next(store_iter)

    def is_directory_page_visible(self) -> bool:
        return self.library_stack.get_visible_child_name() == "directory_page"

    def select_directory_page(self) -> None:
        self.library_stack.set_visible_child_name("directory_page")

    def show_directory(self, uri: str, *, history: bool = False) -> None:
        if uri == self.props.directory_uri:
            return

        if history and (
            len(self._parent_uris) == 0
            or self._parent_uris[-1] != self.props.directory_uri
        ):
            self._parent_uris.append(self.props.directory_uri)

        LOGGER.debug(f"Will show directory with URI {uri!r}")
        LOGGER.debug(f"Parent URIs {self._parent_uris!r}")

        self.props.directory_uri = uri
        self._app.send_message(MessageType.BROWSE_DIRECTORY, {"uri": uri})
        self.select_directory_page()
        self._show_progress_box()

    def goto_parent_state(self) -> None:
        if self.is_directory_page_visible():
            if self.props.directory_uri == "":
                return

            if len(self._parent_uris) > 0:
                self.show_directory(self._parent_uris.pop())
            else:
                LOGGER.warning("Unexpected state!!")
        else:
            self.select_directory_page()

    @Gtk.Template.Callback()
    def directory_view_item_activated_cb(
        self, icon_view: Gtk.IconView, path: Gtk.TreePath
    ) -> None:
        sensitive = self._model.network_available and self._model.connected
        if not sensitive:
            return

        filtered_store = icon_view.get_model()
        store_iter = filtered_store.get_iter(path)
        uri, raw_library_item_type = filtered_store.get(
            store_iter,
            DirectoryStoreColumn.URI,
            DirectoryStoreColumn.TYPE,
        )
        library_item_type = DirectoryItemType(raw_library_item_type)

        LOGGER.debug(f"Selected {library_item_type.name!r} item with URI {uri!r}")

        if library_item_type == DirectoryItemType.ALBUM:
            self._app.send_message(
                MessageType.COMPLETE_ALBUM_DESCRIPTION, {"album_uri": uri}
            )
            self.props.album_details_box.props.uri = uri
            self.props.album_details_box.show_now()
            self.library_stack.set_visible_child_name("album_details_page")
        elif library_item_type == DirectoryItemType.DIRECTORY:
            self.show_directory(uri, history=True)
        elif library_item_type == DirectoryItemType.TRACK:
            self._app.send_message(MessageType.PLAY_TRACKS, {"uris": [uri]})
        elif library_item_type == DirectoryItemType.PLAYLIST:
            pass

    def _on_image_size_changed(
        self,
        settings: Gio.Settings,
        key: str,
    ) -> None:
        image_size = settings.get_int("albums-image-size")
        if image_size == self.image_size:
            return

        self.image_size = image_size
        LOGGER.debug(f"Image size changed to {image_size}")
        self._init_default_images()
        self._update_store_pixbufs(force=True)
        self.directory_view.set_item_width(self.image_size)

    def on_sort_albums_activated(
        self,
        action: Gio.SimpleAction,
        target: GLib.Variant,
    ) -> None:
        sort_id = target.get_string()
        self._settings.set_string("album-sort", sort_id)
        action.set_state(target)