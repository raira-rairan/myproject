"""
オセロゲームのターミナルUI
"""
from .board import Board

class UI:
    """ターミナルUI"""
    
    @staticmethod
    def print_board(game):
        """盤面を表示"""
        board = game.board.board
        valid_moves = game.get_valid_moves()
        
        print("\n     0 1 2 3 4 5 6 7")
        print("   ┌─────────────────┐")
        
        for row in range(8):
            print(f" {row} │", end="")
            for col in range(8):
                if (row, col) in valid_moves:
                    print("·", end=" ")
                elif board[row][col] == Board.EMPTY:
                    print("·", end=" ")
                elif board[row][col] == Board.BLACK:
                    print("●", end=" ")
                elif board[row][col] == Board.WHITE:
                    print("○", end=" ")
            print("│")
        
        print("   └─────────────────┘")
    
    @staticmethod
    def print_score(game):
        """スコアを表示"""
        black_score, white_score = game.get_score()
        current = "黒" if game.current_player == Board.BLACK else "白"
        print(f"\n黒 ● : {black_score}  |  白 ○ : {white_score}")
        print(f"現在のターン: {current}")
    
    @staticmethod
    def print_game_over(game):
        """ゲーム終了画面を表示"""
        black_score, white_score = game.get_score()
        winner = game.get_winner()
        
        print("\n" + "="*40)
        print("ゲーム終了！")
        print("="*40)
        print(f"黒 ● : {black_score}  |  白 ○ : {white_score}")
        
        if winner == Board.BLACK:
            print("🎉 黒の勝利！")
        elif winner == Board.WHITE:
            print("🎉 白の勝利！")
        else:
            print("🤝 同点です！")
    
    @staticmethod
    def get_player_move(game):
        """プレイヤーの入力を取得"""
        valid_moves = game.get_valid_moves()
        
        if not valid_moves:
            print("\n打つ位置がありません。パスします。")
            return None
        
        while True:
            print("\n打つ位置を入力してください (例: 2 3 または 'pass')")
            user_input = input(">> ").strip()
            
            if user_input.lower() == 'pass':
                return 'pass'
            
            try:
                parts = user_input.split()
                if len(parts) != 2:
                    print("2つの数字をスペースで区切ってください")
                    continue
                
                row, col = int(parts[0]), int(parts[1])
                
                if (row, col) not in valid_moves:
                    print(f"その位置には打てません。打てる位置: {valid_moves}")
                    continue
                
                return (row, col)
            
            except ValueError:
                print("数字で入力してください")
