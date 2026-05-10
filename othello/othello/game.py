"""
オセロゲームのロジック
"""
from .board import Board

class Game:
    """オセロゲーム"""
    
    def __init__(self):
        """ゲームを初期化"""
        self.board = Board()
        self.current_player = Board.BLACK
        self.game_over = False
        self.history = []
    
    def make_move(self, row, col):
        """ムーブを実行"""
        if not self.board.place_piece(row, col, self.current_player):
            return False
        
        self.history.append((row, col, self.current_player))
        self._switch_player()
        
        # パスをチェック
        if not self.has_valid_moves():
            self._switch_player()
            if not self.has_valid_moves():
                self.game_over = True
        
        return True
    
    def has_valid_moves(self):
        """現在のプレイヤーが打てる位置があるか"""
        return len(self.board.get_valid_moves(self.current_player)) > 0
    
    def get_valid_moves(self):
        """現在のプレイヤーが打てる位置を取得"""
        return self.board.get_valid_moves(self.current_player)
    
    def pass_turn(self):
        """ターンをパス"""
        self._switch_player()
        
        if not self.has_valid_moves():
            self._switch_player()
            if not self.has_valid_moves():
                self.game_over = True
                return False
        
        return True
    
    def get_score(self):
        """スコアを取得 (黒, 白)"""
        return self.board.count_pieces()
    
    def get_winner(self):
        """ゲーム終了時の勝者を取得"""
        if not self.game_over:
            return None
        
        black_score, white_score = self.get_score()
        
        if black_score > white_score:
            return Board.BLACK
        elif white_score > black_score:
            return Board.WHITE
        else:
            return None  # 同点
    
    def _switch_player(self):
        """プレイヤーを切り替え"""
        self.current_player = Board.WHITE if self.current_player == Board.BLACK else Board.BLACK
