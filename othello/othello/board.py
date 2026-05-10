"""
オセロの盤面管理
"""

class Board:
    """オセロの盤面"""
    
    # 石の状態
    EMPTY = 0
    BLACK = 1
    WHITE = 2
    
    def __init__(self):
        """8x8の盤面を初期化"""
        self.board = [[self.EMPTY for _ in range(8)] for _ in range(8)]
        
        # 初期配置
        self.board[3][3] = self.WHITE
        self.board[3][4] = self.BLACK
        self.board[4][3] = self.BLACK
        self.board[4][4] = self.WHITE
    
    def is_valid_position(self, row, col):
        """座標が盤面内か確認"""
        return 0 <= row < 8 and 0 <= col < 8
    
    def get_piece(self, row, col):
        """座標の石を取得"""
        if self.is_valid_position(row, col):
            return self.board[row][col]
        return None
    
    def set_piece(self, row, col, piece):
        """座標に石を配置"""
        if self.is_valid_position(row, col):
            self.board[row][col] = piece
    
    def count_pieces(self):
        """現在の石の数を返す (黒, 白)"""
        black_count = sum(row.count(self.BLACK) for row in self.board)
        white_count = sum(row.count(self.WHITE) for row in self.board)
        return black_count, white_count
    
    def get_valid_moves(self, player):
        """プレイヤーが打てる位置を取得"""
        valid_moves = []
        
        for row in range(8):
            for col in range(8):
                if self.board[row][col] == self.EMPTY:
                    if self._has_valid_flip(row, col, player):
                        valid_moves.append((row, col))
        
        return valid_moves
    
    def _has_valid_flip(self, row, col, player):
        """その位置に置くと石が裏返るか確認"""
        opponent = self.WHITE if player == self.BLACK else self.BLACK
        directions = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
        
        for dr, dc in directions:
            if self._can_flip_in_direction(row, col, dr, dc, player, opponent):
                return True
        
        return False
    
    def _can_flip_in_direction(self, row, col, dr, dc, player, opponent):
        """指定方向に石が裏返るか確認"""
        r, c = row + dr, col + dc
        has_opponent = False
        
        while self.is_valid_position(r, c):
            piece = self.board[r][c]
            
            if piece == self.EMPTY:
                return False
            elif piece == opponent:
                has_opponent = True
            elif piece == player:
                return has_opponent
            
            r += dr
            c += dc
        
        return False
    
    def place_piece(self, row, col, player):
        """石を配置して、裏返す"""
        if self.board[row][col] != self.EMPTY:
            return False
        
        if not self._has_valid_flip(row, col, player):
            return False
        
        self.board[row][col] = player
        self._flip_pieces(row, col, player)
        
        return True
    
    def _flip_pieces(self, row, col, player):
        """指定位置に置かれた石に挟まれた相手の石を裏返す"""
        opponent = self.WHITE if player == self.BLACK else self.BLACK
        directions = [(-1, -1), (-1, 0), (-1, 1), (0, -1), (0, 1), (1, -1), (1, 0), (1, 1)]
        
        for dr, dc in directions:
            flipped_pieces = self._get_flipped_pieces(row, col, dr, dc, player, opponent)
            for r, c in flipped_pieces:
                self.board[r][c] = player
    
    def _get_flipped_pieces(self, row, col, dr, dc, player, opponent):
        """裏返される石のリストを取得"""
        flipped = []
        r, c = row + dr, col + dc
        
        while self.is_valid_position(r, c):
            piece = self.board[r][c]
            
            if piece == self.EMPTY:
                return []
            elif piece == opponent:
                flipped.append((r, c))
            elif piece == player:
                return flipped
            
            r += dr
            c += dc
        
        return []
    
    def copy(self):
        """盤面をコピー"""
        new_board = Board.__new__(Board)
        new_board.board = [row[:] for row in self.board]
        return new_board
