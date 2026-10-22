# -*- coding: utf-8 -*-
"""GIF 播放。

用 QMovie 边解码边放，不把整个 gif 塞进内存。同一个动作重复用同一个
QMovie 实例，切来切去不用重新解码。
"""

import os

from PySide6.QtCore import QSize
from PySide6.QtGui import QMovie, QPixmap


class Animator:
    def __init__(self, asset_dir):
        self.asset_dir = asset_dir
        self._movies = {}       # gif 文件名 -> QMovie
        self._sizes = {}        # gif 文件名 -> 原始尺寸
        self.current = None
        self.current_file = None
        self.scale = 1.0

    # ------------------------------------------------------------------
    def _get_movie(self, filename):
        movie = self._movies.get(filename)
        if movie is not None:
            return movie

        path = os.path.join(self.asset_dir, filename)
        if not os.path.exists(path):
            return None

        movie = QMovie(path)
        movie.setCacheMode(QMovie.CacheNone)
        if not movie.isValid():
            return None

        movie.jumpToFrame(0)
        size = movie.currentImage().size()
        if size.isEmpty():
            size = QSize(256, 256)
        self._sizes[filename] = size
        self._apply_scale(movie, size)
        self._movies[filename] = movie
        return movie

    def _apply_scale(self, movie, base_size):
        w = max(1, int(base_size.width() * self.scale))
        h = max(1, int(base_size.height() * self.scale))
        movie.setScaledSize(QSize(w, h))

    # ------------------------------------------------------------------
    def play(self, filename):
        """切到某个 gif。同一个就不动。"""
        if filename == self.current_file and self.current is not None:
            return True

        movie = self._get_movie(filename)
        if movie is None:
            return False

        if self.current is not None:
            self.current.stop()

        self.current = movie
        self.current_file = filename
        movie.stop()          # 回到第一帧
        movie.start()
        return True

    def set_scale(self, scale):
        self.scale = float(scale)
        for name, movie in self._movies.items():
            self._apply_scale(movie, self._sizes[name])

    def base_size(self, filename=None):
        filename = filename or self.current_file
        return self._sizes.get(filename, QSize(256, 256))

    def frame(self):
        if self.current is None:
            return QPixmap()
        return self.current.currentPixmap()

    def frame_size(self):
        pix = self.frame()
        if pix.isNull():
            size = self.base_size()
            return QSize(int(size.width() * self.scale), int(size.height() * self.scale))
        return pix.size()

    def preload_all(self, filenames):
        for name in filenames:
            self._get_movie(name)
