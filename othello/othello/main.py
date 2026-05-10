"""
オセロゲームのメインファイル
"""
from .game import Game
from .ui import UI
from .board import Board

def main():
    """ゲームのメインループ"""
    print("="*40)
    print("    オセロゲームへようこそ！")
    print("="*40)
    print("\n[ゲーム説明]")
    print("・黒 ● と白 ○ が交互に石を置きます")
    print("・相手の石を挟むと、その石が自分の色に変わります")
    print("・最後に石が多い方が勝ちです\n")
    
    game = Game()
    
    while not game.game_over:
        UI.print_board(game)
        UI.print_score(game)
        
        # プレイヤーの入力を取得
        move = UI.get_player_move(game)
        
        if move == 'pass':
            if game.pass_turn():
                print("\nターンをパスしました")
            else:
                break
        elif move:
            row, col = move
            game.make_move(row, col)
        else:
            # 打つ位置がない場合
            if game.pass_turn():
                print("\nターンをパスしました")
            else:
                break
    
    # ゲーム終了
    UI.print_board(game)
    UI.print_game_over(game)

if __name__ == '__main__':
    main()
